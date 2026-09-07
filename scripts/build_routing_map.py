"""Сборка карты ответственности из истории: симптом → кто отвечает → что сказать.

  python scripts/build_routing_map.py --db data/hde_bot_vps.db
         [--out data/routing_map.json] [--limit 200] [--pause 4] [--dry-run]

ВНИМАНИЕ про --out: путь по умолчанию (data/routing_map.json) бот читает сразу,
кешируя по mtime. Прогон на проде должен писать КУДА-ТО ЕЩЁ (например
artifacts/), иначе невычитанная карта немедленно попадёт в каждый промпт.

--pause держит прогон под TPM Groq. На free-tier лимит 8000 токенов в минуту, и
один кандидат стоит ~750, то есть пауза 4 с (15 вызовов/мин) его превышает и
отбирает квоту у бота в смену. 8 с безопасно рядом с работающим ботом.

Читает копию прод-базы ТОЛЬКО на чтение и в боевую базу ничего не пишет: карта
должна пройти через глаза человека, потому что она попадает в каждый промпт и
ошибка в ней действует на все тикеты сразу.

Источники:
  1. ai_suggestions с вердиктами bot_escalated / bot_wrong_fact — там, где бот
     разошёлся с оператором, чаще всего и лежит правило маршрутизации;
  2. dialogue_pairs.operator_answer с маршрутизирующей лексикой («обращайтесь в
     банк», «это вопрос к ОФД», «не связано с нашим ПО»).

На выходе два файла: routing_map.json (его читает бот) и routing_map.md для
вычитки. Вычитанный JSON кладётся на прод руками.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DISTILL_SYSTEM = (
    "Ты разбираешь ответы инженера техподдержки кассового оборудования "
    "(Posiflora, АТОЛ, Эвотор, эквайринг, ОФД). Тебе дают ответ оператора "
    "клиенту. Определи, содержит ли он ПРАВИЛО МАРШРУТИЗАЦИИ: указание, что "
    "проблема решается не нами, а другой стороной.\n"
    "Если да, верни JSON:\n"
    '{"routing": true, "symptom": "<с чем обращается клиент, обобщённо, без '
    'имён и номеров>", "owner": "<мы|банк|ОФД|вендор кассы|оператор связи|1С|'
    'клиент сам>", "say": "<что ответить клиенту, одна фраза>"}\n'
    'Если это обычное решение проблемы, верни {"routing": false}.\n'
    "Обобщай симптом так, чтобы правило сработало на другом клиенте: «QR-код на "
    "платёжном терминале», не «QR у ИП Иванова». Никогда не выдумывай адресата, "
    "которого в ответе нет."
)

_SQL_VERDICTS = """
SELECT ticket_id, client_text, judge_reference_answer AS answer
FROM ai_suggestions
WHERE judge_category IN ('bot_escalated', 'bot_wrong_fact')
  AND judge_reference_answer IS NOT NULL AND judge_reference_answer != ''
ORDER BY id DESC LIMIT ?
"""

_SQL_PAIRS = """
SELECT ticket_id, context, operator_answer AS answer
FROM dialogue_pairs
WHERE operator_answer IS NOT NULL AND operator_answer != ''
ORDER BY pair_id DESC LIMIT ?
"""


def collect_rows(db_path: str, limit: int) -> list[dict]:
    """Кандидаты из обоих источников, отфильтрованные по лексике маршрутизации.

    Лексический фильтр стоит ДО LLM намеренно: пар в базе под 28 тысяч, и гнать
    их все через модель — часы и вся суточная квота Groq. Маршрутизирующих
    ответов среди них единицы процентов.
    """
    from bot.agent.routing_map import looks_like_routing

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        for sql, question_col in ((_SQL_VERDICTS, "client_text"), (_SQL_PAIRS, "context")):
            for row in conn.execute(sql, (limit,)):
                answer = (row["answer"] or "").strip()
                if not answer or not looks_like_routing(answer):
                    continue
                key = " ".join(answer.lower().split())[:200]
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "ticket_id": row["ticket_id"],
                    "question": (row[question_col] or "")[-600:],
                    "answer": answer[:1200],
                })
    finally:
        conn.close()
    return rows


async def distill(rows: list[dict], *, pause_s: float = 4.0) -> list[dict]:
    """Прогон кандидатов через LLM. Пейсинг под free-tier Groq (TPM 8000)."""
    from bot.ai_summary import call_groq_json
    from bot.config import config

    out: list[dict] = []
    for i, row in enumerate(rows):
        if i:
            await asyncio.sleep(pause_s)
        user = (
            (f"Вопрос клиента:\n{row['question']}\n\n" if row["question"] else "")
            + f"Ответ оператора:\n{row['answer']}"
        )
        try:
            raw = await call_groq_json(
                _DISTILL_SYSTEM, user, model=config.agent_selfcheck_model
            )
            obj = json.loads(raw)
        except Exception as exc:
            print(f"  ! тикет {row['ticket_id']}: {exc}")
            continue
        if not isinstance(obj, dict) or not obj.get("routing"):
            continue
        obj["ticket_id"] = row["ticket_id"]
        out.append(obj)
        print(f"  + {obj.get('symptom', '')[:60]} → {obj.get('owner', '')}")
    return out


def to_markdown(rules) -> str:
    lines = [
        "# Карта ответственности (черновик для вычитки)",
        "",
        "Проверь каждую строку: она попадёт в КАЖДЫЙ промпт. Ошибка здесь "
        "действует на все тикеты сразу.",
        "",
        "| Симптом | Отвечает | Что сказать клиенту |",
        "|---|---|---|",
    ]
    for rule in rules:
        lines.append(f"| {rule.symptom} | {rule.owner} | {rule.say} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="копия прод-базы (открывается ro)")
    parser.add_argument("--out", default="data/routing_map.json")
    parser.add_argument("--limit", type=int, default=200,
                        help="сколько строк брать из каждого источника")
    parser.add_argument("--pause", type=float, default=8.0,
                        help="пауза между вызовами модели, секунды (TPM Groq)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать кандидатов, не звать модель")
    args = parser.parse_args()

    from bot.agent.routing_map import dedup_rules, parse_routing_map

    rows = collect_rows(args.db, args.limit)
    print(f"Кандидатов по лексике: {len(rows)}")
    if args.dry_run:
        for row in rows[:40]:
            print(f"  [{row['ticket_id']}] {row['answer'][:120]}")
        return
    if not rows:
        return

    raw_rules = await distill(rows, pause_s=args.pause)
    rules = dedup_rules(parse_routing_map(raw_rules))
    print(f"Правил после дедупа: {len(rules)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"rules": [{"symptom": r.symptom, "owner": r.owner, "say": r.say}
                       for r in rules]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    md = out.with_suffix(".md")
    md.write_text(to_markdown(rules), encoding="utf-8")
    print(f"Записано: {out} и {md}")
    print("ВЫЧИТАЙ .md перед тем, как класть JSON на прод: карта идёт в каждый промпт.")


if __name__ == "__main__":
    asyncio.run(main())

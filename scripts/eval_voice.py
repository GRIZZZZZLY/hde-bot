"""Офлайн-проверка агента v2 против старого пути (spec 2026-09-27 §7).

Запуск на сервере (там прод-БД, модель эмбеддингов и ключ Groq):
  python scripts/eval_voice.py build --n 50     # набор из прод-БД + посты из HDE
  python scripts/eval_voice.py run [--side new] [--start N --count M]  # частями: TPD 200k/сутки
  python scripts/eval_voice.py report           # счётчики + слепые пары для A/B

Генерация идёт через настоящий код агента (build_agent_context +
generate_agent_draft), а не через упрощённый вызов: иначе проверка мерит не тот
пайплайн (урок разбора 2026-09-27, §3.9).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SET_PATH = ROOT / "data" / "golden" / "voice_v2_set.json"
RUN_PATH = ROOT / "artifacts" / "voice_eval.json"
PAIRS_PATH = ROOT / "artifacts" / "voice_ab_pairs.json"
KEY_PATH = ROOT / "artifacts" / "voice_ab_key.json"

_REMOTE = re.compile(r"anydesk|rudesktop|анидеск|рудесктоп|удал[её]нн", re.I)
_WAIT = re.compile(r"инженер|специалист|свяжет|ожидайте|ждите|звонк|передад", re.I)
_CHECK = re.compile(r"получилось|сработало|заработал|проверьте,? (?:пожалуйста,? )?сейчас", re.I)
# 8000 TPM на qwen3.8 — окно в минуту, а один запрос ~6k токенов (старый путь до ~8k):
# два запроса в одну минуту уже за лимитом. 65 с оставляет запас живому боту.
_PACE_S = 65


def pattern_flags(text: str) -> dict[str, bool]:
    text = text or ""
    return {
        "remote": bool(_REMOTE.search(text)),
        "wait": bool(_WAIT.search(text)),
        "check_back": bool(_CHECK.search(text)),
        "multi_question": text.count("?") > 1,
    }


def summarize(rows: list[dict]) -> dict:
    out = {}
    for side in ("old", "new"):
        # сбой генерации — не «чистый черновик»: из долей его убираем, считаем отдельно
        scored = [r.get(side) or {} for r in rows if not (r.get(side) or {}).get("failed")]
        n = max(len(scored), 1)
        flags = [pattern_flags(s.get("client", "")) for s in scored]
        out[side] = {k: round(sum(f[k] for f in flags) / n, 3) for k in flags[0]} if flags else {}
        hard = [len((s.get("lint") or {}).get("hard", [])) for s in scored]
        out[side]["hard_lint_share"] = round(sum(1 for h in hard if h) / n, 3)
        out[side]["failed"] = len(rows) - len(scored)
        out[side]["n"] = len(scored)
    return out


def make_ab_pairs(rows: list[dict], seed: int = 42) -> tuple[list[dict], dict]:
    rnd = random.Random(seed)
    pairs, key = [], {}
    for r in rows:
        old, new = (r["old"] or {}).get("client", ""), (r["new"] or {}).get("client", "")
        if rnd.random() < 0.5:
            pairs.append({"case_id": r["case_id"], "A": old, "B": new})
            key[str(r["case_id"])] = "A=old"
        else:
            pairs.append({"case_id": r["case_id"], "A": new, "B": old})
            key[str(r["case_id"])] = "A=new"
    return pairs, key


async def build(n: int, db_path: str) -> None:
    from bot.hde_api import HDEApiClient, HDEApiError, post_sort_key
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id, ticket_id, title, context_until_post_id, judge_reference_answer "
        "FROM ai_suggestions WHERE judge_reference_answer IS NOT NULL "
        "AND judge_reference_answer != '' AND context_until_post_id IS NOT NULL "
        "AND context_until_post_id != '' ORDER BY id DESC"
    ).fetchall()
    seen, cases = set(), []
    client = HDEApiClient()
    for sid, ticket_id, title, anchor, reference in rows:
        if ticket_id in seen:
            continue
        if len(cases) >= n:
            break
        seen.add(ticket_id)
        try:
            info = await client.get_ticket_info(str(ticket_id))
            posts = await client.get_ticket_posts(str(ticket_id))
            try:
                comments = await client.get_ticket_comments(str(ticket_id))
            except HDEApiError:
                comments = []
            cut = int(anchor or 0)
            kept = sorted((p for p in posts + comments if int(p.post_id) <= cut),
                         key=post_sort_key)
            cases.append({
                "case_id": sid, "ticket_id": str(ticket_id), "title": title or "",
                "reference": reference,
                "info": {"client_id": info.client_id, "client_name": info.client_name,
                         "owner_id": info.owner_id, "owner_name": info.owner_name},
                "posts": [{"post_id": p.post_id, "user_id": p.user_id, "text": p.text,
                           "date_created": p.date_created, "is_comment": p.is_comment}
                          for p in kept],
            })
        except Exception as exc:
            print(f"skip ticket {ticket_id}: {exc}")
        await asyncio.sleep(1.2)                  # HDE: 300 req/min на весь аккаунт
    SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SET_PATH.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {len(cases)} cases → {SET_PATH}")


async def _one(case: dict, v2: bool) -> dict:
    from bot.agent.context import build_agent_context
    from bot.agent.generate import generate_agent_draft
    from bot.agent.lint import check_draft
    from bot.config import config
    from bot.hde_api import HDEPost, HDETicketInfo

    config.agent_voice_v2_enabled = v2        # обычный изменяемый экземпляр, как в тестах
    posts = [HDEPost(**p) for p in case["posts"]]
    info = HDETicketInfo(**case["info"])
    ctx = await build_agent_context(posts, info, case["title"], ticket_id=case["ticket_id"])
    draft = await generate_agent_draft(ctx, case["title"])
    if draft is None:
        return {"failed": True, "client": "", "memo": "", "analysis": "", "action": None,
                "lint": {"fixed": [], "hard": [], "soft": []}}
    client = draft.get("client", "")
    lint = check_draft(                       # те же входы, что в pipeline.run_agent
        client, draft.get("memo", ""), history=ctx["history"],
        sources_text="\n".join(
            e.get("used_excerpt", "") for e in ctx["evidence"] + ctx.get("demos", [])),
        facts="\n".join([ctx.get("ticket_facts", ""), ctx.get("attachments", ""),
                         ctx.get("call_notes", "")]),
        first_staff_reply=ctx.get("first_staff_reply", False),
        grounds=ctx.get("grounds", []),
        source_ids=draft.get("source_ids", []) if client else [],
    )
    return {"action": draft.get("action"), "client": draft.get("client", ""),
            "memo": draft.get("memo", ""), "analysis": draft.get("analysis", ""),
            "lint": lint.as_dict()}


async def _noop(*_a, **_k) -> None:
    return None


async def run(side: str = "both", start: int = 0, count: int | None = None,
              pace_s: float = _PACE_S) -> None:
    """Прогон части набора. У Groq дневной лимит 200k токенов на qwen3.8 на весь
    аккаунт — весь набор за день не пройти. Поэтому side="new" перегенерирует
    только новую версию, а готовые ответы старой берёт из прошлого прогона;
    результат сохраняется после каждого кейса, прерванный прогон не теряется."""
    import bot.db as _db
    # build_agent_context отмечает last_used_at у статей — прогон проверки не
    # должен продлевать им жизнь в прод-БД (архивация автоправил смотрит на него)
    _db.update_knowledge_last_used = _noop
    cases = json.loads(SET_PATH.read_text(encoding="utf-8"))["cases"]
    chosen = cases[start:start + count] if count else cases[start:]
    rows = ({r["case_id"]: r for r in json.loads(RUN_PATH.read_text(encoding="utf-8"))}
            if RUN_PATH.exists() else {})
    sides = (("old", False), ("new", True)) if side == "both" else (("new", True),)
    RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    first = True
    for i, case in enumerate(chosen):
        row = rows.get(case["case_id"]) or {
            "case_id": case["case_id"], "ticket_id": case["ticket_id"],
            "reference": case["reference"], "old": {"failed": True, "client": ""},
        }
        for name, v2 in sides:
            if not first:
                await asyncio.sleep(pace_s)
            first = False
            row[name] = await _one(case, v2)
        rows[case["case_id"]] = row
        ordered = [rows[c["case_id"]] for c in cases if c["case_id"] in rows]
        RUN_PATH.write_text(json.dumps(ordered, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{i + 1}/{len(chosen)}", flush=True)


def report() -> None:
    rows = json.loads(RUN_PATH.read_text(encoding="utf-8"))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
    pairs, key = make_ab_pairs(rows)
    PAIRS_PATH.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    KEY_PATH.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"A/B pairs → {PAIRS_PATH} (key: {KEY_PATH})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--n", type=int, default=50)
    b.add_argument("--db", default=str(ROOT / "hde_bot.db"))
    r = sub.add_parser("run")
    r.add_argument("--side", choices=("both", "new"), default="both")
    r.add_argument("--start", type=int, default=0)
    r.add_argument("--count", type=int, default=None)
    r.add_argument("--pace", type=float, default=_PACE_S,
                   help="пауза между запросами, с (Groq: 65; другой провайдер: меньше)")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "build":
        asyncio.run(build(a.n, a.db))
    elif a.cmd == "run":
        asyncio.run(run(a.side, a.start, a.count, a.pace))
    else:
        report()


if __name__ == "__main__":
    main()

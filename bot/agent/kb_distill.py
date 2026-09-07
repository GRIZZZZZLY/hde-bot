"""Выжимка правила из вердикта сверки — вместо ручного «в базу / мимо» каждое утро.

Кандидат в базу знаний раньше был сырым диалогом: тема, история и дословный
ответ оператора. Решал человек, по каждому. На проде это дало очередь 19 pending
при 2 решённых, и большинство строк правилом не является вовсе («Да, забыть
устройство», «Нет, обновляю драйвер»). Такую работу нельзя доделать: она
приходит быстрее, чем разбирается.

Здесь она разбирается автоматически, и человеку остаётся один класс:

    выжимка → правило или ничего
    правило → похоже на уже лежащее в базе?
        нет            → добавляем сами, отдельным источником и низким весом
        дубль          → ничего не пишем
        уточняет       → добавляем сами
        противоречит   → показываем человеку, это единственная кнопка

Гарантия здесь не «человек проверил», а «отдельный источник auto_rule, низкий
вес, архивация по неиспользованию и видимый счётчик в утренней сводке». Модель
всё ещё может ошибиться — но ошибка видна, обратима и не смешана с базой из
закрытых тикетов.
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import re

logger = logging.getLogger(__name__)

RULE_SOURCE = "auto_rule"
RULE_QUALITY = "auto_rule"

# Порог «это про то же самое». Ниже — считаем правило новым и добавляем.
# Взят выше RAG_MIN_SCORE (0.83) намеренно: там задача «найти похожее для
# промпта», здесь — «не завести второе правило о том же», и цена ошибки разная.
RULE_DUP_SCORE = 0.90

_RELATIONS = ("duplicate", "refines", "contradicts")

_WS_RE = re.compile(r"\s+")

_DISTILL_SYSTEM = (
    "Ты ведёшь базу знаний техподдержки кассового оборудования (Posiflora, "
    "АТОЛ, Эвотор, эквайринг, ОФД, фискальные накопители). Тебе дают вопрос "
    "клиента, черновик ассистента и то, что реально ответил инженер.\n"
    "Найди в ответе инженера ОБОБЩАЕМОЕ правило: знание, которое сработает на "
    "другом клиенте с тем же симптомом.\n"
    'Верни СТРОГО JSON: {"generalizable": true|false, "symptom": "<с чем '
    'обратился клиент, обобщённо, без имён, номеров и названий компаний>", '
    '"rule": "<сам факт одним предложением>", "action": "<что делать '
    'оператору или клиенту, одна фраза>"}\n'
    "generalizable=false, если ответ инженера — подтверждение («да», «нет»), "
    "разовое действие по этому клиенту, обслуживание сеанса связи или что-то, "
    "что в другом тикете не пригодится. Это нормальный и частый исход.\n"
    "Ничего не выдумывай: правило должно целиком следовать из ответа инженера."
)

_OVERLAP_SYSTEM = (
    "Ты сверяешь два правила из базы знаний техподдержки. Определи отношение "
    "НОВОГО правила к УЖЕ СУЩЕСТВУЮЩЕМУ.\n"
    "duplicate — то же самое другими словами, нового знания нет.\n"
    "refines — новое уточняет существующее (частный случай, конкретная модель "
    "или банк), не отменяя его.\n"
    "contradicts — новое утверждает обратное: другой адресат, другая причина, "
    "другое значение параметра.\n"
    'Верни СТРОГО JSON: {"relation":"duplicate|refines|contradicts",'
    '"reason":"кратко по-русски"}'
)


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def rule_content_hash(rule: dict) -> str:
    """Хеш по нормализованному тексту правила.

    Одно и то же правило, выжатое из двух тикетов, должно апсертиться в одну
    строку, а не размножаться. Регистр и пробелы для этого несущественны.
    """
    return hashlib.sha256(_norm(rule.get("rule", "")).encode("utf-8")).hexdigest()


def build_rule_content(rule: dict, *, ticket_id: str = "") -> str:
    """Содержимое статьи-правила: три строки и ссылка на тикет.

    Диалог в контент не идёт намеренно. Он портит эмбеддинг (там доминируют
    приветствия и обслуживание сеанса) и мешает человеку, который открыл строку
    в базе, чтобы прочесть правило, а не переписку.
    """
    lines = [
        f"Симптом: {(rule.get('symptom') or '').strip()}",
        f"Правило: {(rule.get('rule') or '').strip()}",
    ]
    action = (rule.get("action") or "").strip()
    if action:
        lines.append(f"Что делать: {action}")
    if ticket_id:
        lines.append(f"Источник: тикет {ticket_id}")
    return "\n".join(lines)


async def distill_rule(
    *, client_text: str = "", ai_answer: str = "", reference: str = "",
    title: str = "", history: str = "", _call_fn=None,
) -> dict | None:
    """Правило из пары «вопрос клиента ↔ ответ оператора», либо None.

    None означает «правилом не является» и закрывает кандидата без участия
    человека. Это самый частый исход, и он нормальный.
    """
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    parts = []
    if title:
        parts.append(f"Тема тикета: {title[:200]}")
    if client_text:
        parts.append(f"Вопрос клиента:\n{client_text[:800]}")
    elif history:
        parts.append(f"История:\n{history[-800:]}")
    parts.append(f"Черновик ассистента:\n{(ai_answer or '')[:800]}")
    parts.append(f"Ответ инженера:\n{(reference or '')[:1200]}")

    try:
        raw = await _call_fn(
            _DISTILL_SYSTEM, "\n\n".join(parts), model=config.agent_selfcheck_model
        )
        obj = _json.loads(raw)
    except Exception as exc:
        logger.debug("kb distill: выжимка не удалась: %s", exc)
        return None
    if not isinstance(obj, dict) or not obj.get("generalizable"):
        return None
    rule = (obj.get("rule") or "").strip()
    symptom = (obj.get("symptom") or "").strip()
    if not rule or not symptom:
        # generalizable=true с пустым правилом — брак модели, а не правило.
        return None
    return {"symptom": symptom, "rule": rule,
            "action": (obj.get("action") or "").strip()}


async def classify_overlap(
    new_rule: str, existing_rule: str, *, _call_fn=None
) -> tuple[str, str]:
    """Отношение нового правила к существующему.

    При любой неопределённости — contradicts, то есть «показать человеку».
    Показать лишнее правило дешевле, чем автоматически записать неверное:
    записанное попадёт в промпт и будет молча портить черновики.
    """
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    user = f"Новое правило:\n{new_rule[:800]}\n\nСуществующее правило:\n{existing_rule[:800]}"
    try:
        raw = await _call_fn(_OVERLAP_SYSTEM, user, model=config.agent_selfcheck_model)
        obj = _json.loads(raw)
    except Exception as exc:
        logger.debug("kb distill: сверка правил не удалась: %s", exc)
        return "contradicts", "сверка не удалась — на решение человеку"
    if not isinstance(obj, dict) or obj.get("relation") not in _RELATIONS:
        return "contradicts", "непонятное отношение — на решение человеку"
    return obj["relation"], str(obj.get("reason", ""))[:200]


async def find_similar_rule(rule_text: str) -> dict | None:
    """Ближайшее уже накопленное правило, если оно ближе RULE_DUP_SCORE."""
    from ..knowledge.indexer import embed_text
    from ..knowledge.store import find_similar

    embedding = await embed_text(rule_text, task_type="query")
    if embedding is None:
        return None
    hits = await find_similar(embedding, limit=1, sources={RULE_SOURCE})
    for item, score in hits:
        if score >= RULE_DUP_SCORE:
            return {"id": item.id, "content": item.content, "score": float(score)}
    return None


async def process_candidate(
    candidate: dict, *, _distill_fn=None, _similar_fn=None, _overlap_fn=None,
    _index_fn=None, _status_fn=None,
) -> str | None:
    """Провести одного кандидата до конца. Возвращает исход или None.

    None — по кандидату уже решили: статус переводится из pending первым же
    вызовом, и повторный проход джоба не запишет правило второй раз.
    """
    if _distill_fn is None:
        _distill_fn = distill_rule
    if _similar_fn is None:
        _similar_fn = find_similar_rule
    if _overlap_fn is None:
        _overlap_fn = classify_overlap
    if _index_fn is None:
        from ..knowledge.indexer import index_knowledge_item as _index_fn
    if _status_fn is None:
        from ..db import set_kb_candidate_status as _status_fn

    cid = candidate["id"]
    rule = await _distill_fn(
        client_text=candidate.get("client_text") or "",
        ai_answer=candidate.get("ai_answer") or "",
        reference=candidate.get("reference_answer") or "",
        title=candidate.get("title") or "",
        history=candidate.get("history") or "",
    )
    if rule is None:
        row = await _status_fn(cid, "not_generalizable", kind="rule")
        return "not_generalizable" if row is not None else None

    rule_json = _json.dumps(rule, ensure_ascii=False)
    existing = await _similar_fn(rule["rule"])
    relation, reason = "new", ""
    if existing is not None:
        relation, reason = await _overlap_fn(rule["rule"], existing["content"])

    if relation == "duplicate":
        row = await _status_fn(cid, "duplicate", kind="rule", rule_json=rule_json,
                               conflict_item_id=existing["id"])
        return "duplicate" if row is not None else None

    if relation == "contradicts":
        # Единственный случай для человека: база уже утверждает обратное, и
        # автоматически выбрать сторону нельзя — обе строки чем-то обоснованы.
        row = await _status_fn(cid, "conflict", kind="rule", rule_json=rule_json,
                               conflict_item_id=existing["id"])
        return "conflict" if row is not None else None

    # new / refines — добавляем сами.
    row = await _status_fn(cid, "auto_added", kind="rule", rule_json=rule_json,
                           conflict_item_id=existing["id"] if existing else None)
    if row is None:
        return None
    ticket_id = str(candidate.get("ticket_id") or "")
    content = build_rule_content(rule, ticket_id=ticket_id)
    if relation == "refines" and existing is not None:
        content += f"\nУточняет правило #{existing['id']}"
    await _index_fn(
        source=RULE_SOURCE,
        content=content,
        ticket_id=ticket_id,
        title=rule["symptom"][:120],
        quality=RULE_QUALITY,
        content_hash=rule_content_hash(rule),
    )
    logger.info(
        "kb distill: правило из тикета %s (%s): %s", ticket_id, relation,
        rule["rule"][:80],
    )
    return "auto_added"


async def process_pending_candidates(
    *, limit: int = 20, pause_s: float = 4.0,
    _list_fn=None, _process_fn=None, _sleep_fn=None,
) -> dict:
    """Разобрать очередь кандидатов. Ошибка по одному не роняет проход.

    Пейсинг под free-tier Groq: на кандидата уходит один или два вызова модели.
    """
    if _list_fn is None:
        from ..db import list_pending_kb_candidates as _list_fn
    if _process_fn is None:
        _process_fn = process_candidate
    if _sleep_fn is None:
        import asyncio
        _sleep_fn = asyncio.sleep

    stats = {"auto_added": 0, "duplicate": 0, "not_generalizable": 0,
             "conflict": 0, "errors": 0}
    for i, candidate in enumerate(await _list_fn(limit)):
        try:
            if i and pause_s:
                await _sleep_fn(pause_s)
            outcome = await _process_fn(candidate)
            if outcome in stats:
                stats[outcome] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning(
                "kb distill: кандидат %s не обработан: %s", candidate.get("id"), exc
            )
    return stats

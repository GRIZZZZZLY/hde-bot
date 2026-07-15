"""Ночная сверка: предложенный ботом ответ ↔ фактический ответ оператора в тикете.

Операторы не жмут кнопки бота и не отвечают через него (см. память
optimization-samples-stale) — поэтому сигнал для обучения берём иначе: после смены
сверяем, что бот предложил, с тем, что оператор реально написал клиенту в тикете.
Ноль участия операторов, человеческий фактор исключён.

Ядро (этот модуль) чистое и тестируемое; обвязка (БД/HDE API/расписание) — в
reconcile_recent + scheduler.
"""
from __future__ import annotations

import difflib
import html as _html
import re as _re

from .dialogue_mining import is_staff_post, sort_posts

# Ниже этого порога считаем, что бот разошёлся с оператором → эталон = человек.
MATCH_THRESHOLD = 0.6

# Порог для косинуса e5-эмбеддингов: у e5 «пол» ~0.7-0.8 даже на несвязанных
# текстах, парафразы уходят к 0.9+. Калибруется по накопленным парам.
SEMANTIC_MATCH_THRESHOLD = 0.85

# Штампы бота/первой линии/диспетчера в тикете — не «ответ оператора».
_BOILERPLATE_RE = _re.compile(
    r"принят[оа] в работу|передан[оа].*специалист|свяжется с вами|"
    r"специалист свяжется|ожидайте.*(ответ|чат)|благодарим за (информаци|ожидани|предостав)",
    _re.I,
)

# Закрывашки в конце ответа — вырезаются фразой (не строкой): часто приклеены
# к содержательному тексту. Ответ из одних закрывашек → None (тикет решён вне
# переписки, судить не по чему).
_CLOSER_RE = _re.compile(
    r"(?:подскажите,?\s*)?могу (?:вам )?[её]щ[её] чем-то помочь\s*\??|"
    r"всегда рады помочь[!.]?|"
    r"будут [её]щ[её] вопросы\s*[—–-]?\s*обращайтесь[!.]?",
    _re.I,
)


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def _clean_answer(text: str) -> str:
    """Убирает строки-штампы и фразы-закрывашки, оставляет содержательный текст."""
    kept = [
        ln.strip()
        for ln in (text or "").splitlines()
        if ln.strip() and not _BOILERPLATE_RE.search(ln)
    ]
    joined = _CLOSER_RE.sub(" ", " ".join(kept))
    return _re.sub(r"\s+", " ", joined).strip()


def _post_id(post) -> int:
    try:
        return int(getattr(post, "post_id", 0))
    except (TypeError, ValueError):
        return 0


def find_operator_reply_after(posts, anchor_post_id, staff: set[str]) -> str | None:
    """Первый содержательный ответ оператора ПОСЛЕ якоря (context_until_post_id).

    Якорь — последний пост, что бот видел при генерации. Реальный ответ оператора —
    первый staff-turn с post_id > якоря (подряд идущие staff-посты склеиваются).
    Штампы отфильтровываются; если содержательного ответа нет — None (тикет ещё в
    работе / брошен → не судим).
    """
    try:
        anchor = int(anchor_post_id)
    except (TypeError, ValueError):
        anchor = 0
    ordered = [
        p for p in sort_posts(posts)
        if not getattr(p, "is_comment", False) and _post_id(p) > anchor
    ]
    i, n = 0, len(ordered)
    while i < n and not is_staff_post(ordered[i], staff):
        i += 1
    turn = []
    while i < n and is_staff_post(ordered[i], staff):
        turn.append(ordered[i])
        i += 1
    if not turn:
        return None
    text = _clean_answer("\n".join(_strip_html(p.text) for p in turn))
    return text or None


def compare(ai_answer: str, reference: str) -> tuple[str, float]:
    """Лексическое сравнение (фолбэк без модели). Возвращает (label, score 0..1)."""
    score = difflib.SequenceMatcher(
        None, (ai_answer or "").lower(), (reference or "").lower()
    ).ratio()
    label = "matched" if score >= MATCH_THRESHOLD else "diverged"
    return label, round(score, 4)


async def compare_semantic(
    ai_answer: str, reference: str, *, _embed_fn=None
) -> tuple[str, float]:
    """Семантическое сравнение на RAG-эмбеддингах (e5, cosine = dot: векторы
    нормализованы). При недоступности модели — лексический фолбэк."""
    if _embed_fn is None:
        from ..knowledge.indexer import embed_texts as _embed_fn
    vecs = await _embed_fn([ai_answer or "", reference or ""], task_type="query")
    if not vecs or len(vecs) != 2:
        return compare(ai_answer, reference)
    score = float(vecs[0] @ vecs[1])
    label = "matched" if score >= SEMANTIC_MATCH_THRESHOLD else "diverged"
    return label, round(score, 4)


async def reconcile_one(
    *, ai_answer: str, posts, anchor_post_id, staff: set[str], _embed_fn=None
) -> dict | None:
    """Сверка одного предложения. None, если реального ответа оператора ещё нет."""
    reference = find_operator_reply_after(posts, anchor_post_id, staff)
    if not reference:
        return None
    label, score = await compare_semantic(ai_answer, reference, _embed_fn=_embed_fn)
    return {"reference_answer": reference, "label": label, "score": score}


# matched = бот ≈ человек (accepted); diverged = человек иначе → эталон = человек (corrected).
_LABEL_MAP = {"matched": "accepted", "diverged": "corrected"}


async def reconcile_recent(
    *, hours: int = 24, _suggestions_fn=None, _posts_fn=None, _set_fn=None,
    _staff=None, _embed_fn=None,
) -> dict:
    """Ночная сверка свежих предложений с фактическими ответами операторов.

    Тянет посты через HDE API (под троттлом), пишет judge_reference_answer +
    канонический judge_label (accepted/corrected). Возвращает счётчики для сводки.
    Ошибка по одному тикету не роняет проход.
    """
    import logging

    logger = logging.getLogger(__name__)
    from ..config import config

    if _suggestions_fn is None:
        from ..db import get_unjudged_suggestions as _suggestions_fn
    if _set_fn is None:
        from ..db import set_judge_result as _set_fn
    if _posts_fn is None:
        from ..hde_api import HDEApiClient
        _client = HDEApiClient()

        async def _posts_fn(ticket_id):
            return await _client.get_all_ticket_posts(ticket_id)

    staff = set(_staff) if _staff is not None else {
        str(config.hde_owner_id), *config.agent_staff_user_ids
    }

    stats = {"matched": 0, "diverged": 0, "skipped": 0, "errors": 0}
    for sug in await _suggestions_fn(hours=hours):
        try:
            posts = await _posts_fn(str(sug["ticket_id"]))
            res = await reconcile_one(
                ai_answer=sug.get("ai_answer") or "",
                posts=posts,
                anchor_post_id=sug.get("context_until_post_id"),
                staff=staff,
                _embed_fn=_embed_fn,
            )
            if res is None:
                stats["skipped"] += 1
                continue
            await _set_fn(
                sug["id"],
                reference_answer=res["reference_answer"],
                label=_LABEL_MAP[res["label"]],
                detail=f"reconcile:{res['label']} score={res['score']}",
            )
            stats["matched" if res["label"] == "matched" else "diverged"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("reconcile: ticket %s failed: %s", sug.get("ticket_id"), exc)
    return stats

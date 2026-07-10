"""Нарезка закрытых тикетов на пары «контекст → ответ оператора» (Phase 2A, rev.2).

Явная сортировка старых→новым; последовательные ответы оператора склеиваются в
один turn; контекст строго до ответа (без утечки будущего); content_hash включает
нормализованный ответ; эмбеддинг строится по problem-side."""
from __future__ import annotations

import hashlib
import html as _html
import re as _re

_EMBED_MODEL = "intfloat/multilingual-e5-large"


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def _norm(text: str) -> str:
    return _re.sub(r"\s+", " ", (text or "").lower()).strip()


def _seq_key(post):
    try:
        pid = int(getattr(post, "post_id", 0))
    except (TypeError, ValueError):
        pid = 0
    return (pid, getattr(post, "date_created", "") or "")


def sort_posts(posts) -> list:
    return sorted(posts, key=_seq_key)


def staff_id_set(owner_id: str, staff_ids: tuple[str, ...]) -> set[str]:
    if staff_ids:
        return {str(s) for s in staff_ids}
    return {str(owner_id)}


def is_staff_post(post, staff: set[str]) -> bool:
    """Роль автора: поле типа из HDE JSON, если оно есть (см. Task 0), иначе
    членство в staff-id. Боты/системные — не staff и не клиент (исключаются выше)."""
    # Task 0 governs: if HDEPost gains an author-type attr, prefer it here.
    return str(getattr(post, "user_id", "")) in staff


def _pair_content_hash(ticket_id, operator_message_id, context, operator_answer) -> str:
    raw = f"{ticket_id}|{operator_message_id}|{_norm(context)}|{_norm(operator_answer)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_ticket_into_pairs(ticket_id: str, posts: list, staff: set[str]) -> list[dict]:
    """Пары для каждого клиент→оператор перехода; ответ = склейка подряд идущих
    staff-постов. Контекст — только посты ДО turn'а оператора."""
    ordered = [p for p in sort_posts(posts) if not getattr(p, "is_comment", False)]
    pairs: list[dict] = []
    i = 0
    n = len(ordered)
    while i < n:
        if not is_staff_post(ordered[i], staff):
            i += 1
            continue
        # начало staff-turn'а: склеиваем подряд идущие staff-посты
        turn_start = i
        turn_posts = []
        while i < n and is_staff_post(ordered[i], staff):
            turn_posts.append(ordered[i])
            i += 1
        prior = ordered[:turn_start]
        client_prior = [p for p in prior if not is_staff_post(p, staff)]
        client_lines = [_strip_html(p.text) for p in client_prior]
        client_lines = [t for t in client_lines if t]
        if not client_lines:
            continue  # нет клиентского контекста до ответа
        operator_answer = "\n".join(
            t for t in (_strip_html(p.text) for p in turn_posts) if t
        )
        if not operator_answer:
            continue
        context_lines = []
        for p in prior:
            text = _strip_html(p.text)
            if not text:
                continue
            role = "Оператор" if is_staff_post(p, staff) else "Клиент"
            context_lines.append(f"{role}: {text}")
        context = "\n".join(context_lines)
        op_msg_id = str(turn_posts[0].post_id)
        content_hash = _pair_content_hash(ticket_id, op_msg_id, context, operator_answer)
        pairs.append({
            "ticket_id": ticket_id,
            "source_message_id": str(client_prior[-1].post_id),
            "context_until_message_id": str(prior[-1].post_id),
            "operator_message_id": op_msg_id,
            "operator_answer_at": getattr(turn_posts[0], "date_created", None),
            "context": context,
            "operator_answer": operator_answer,
            "content_hash": content_hash,
        })
    return pairs

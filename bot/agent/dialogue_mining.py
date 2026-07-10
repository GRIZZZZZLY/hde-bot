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
            "operator_user_id": str(turn_posts[0].user_id),
            "operator_answer_at": getattr(turn_posts[0], "date_created", None),
            "context": context,
            "operator_answer": operator_answer,
            "content_hash": content_hash,
        })
    return pairs


async def resolve_staff_ids(
    client, user_ids: set[str], *, base: set[str], cache: dict
) -> set[str]:
    """Дополняет staff-set динамически: неизвестные user_id резолвятся через
    HDE `GET /users/{id}` (group.type == 'staff'), результат кэшируется.
    Команда поддержки в HDE ~10 человек и меняется — статический env-список
    ломается на новом сотруднике; резолвер закрывает это."""
    staff = set(base)
    for uid in user_ids:
        uid = str(uid)
        if uid in staff:
            continue
        if uid not in cache:
            try:
                cache[uid] = await client.get_user_group_type(uid)
            except Exception:
                cache[uid] = None
        if cache[uid] == "staff":
            staff.add(uid)
    return staff


def build_embedding_text(pair: dict) -> str:
    """Problem-side текст для эмбеддинга: последний вопрос клиента + контекст."""
    client_lines = [
        ln for ln in pair["context"].splitlines() if ln.startswith("Клиент:")
    ]
    last_client = client_lines[-1][len("Клиент:"):].strip() if client_lines else ""
    return f"Вопрос клиента: {last_client}\nКонтекст: {pair['context'][-800:]}"


def _embedding_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def mine_ticket_pairs(
    client, ticket: dict, staff: set[str], *,
    known_hashes: set[str] | None = None, staff_cache: dict | None = None,
    _embed_fn=None, _save_fn=None,
) -> int:
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _save_fn is None:
        from ..db import save_dialogue_pair as _save_fn
    known = known_hashes if known_hashes is not None else set()

    ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
    if not ticket_id:
        return 0
    posts = await client.get_all_ticket_posts(ticket_id)
    if staff_cache is not None:
        authors = {str(getattr(p, "user_id", "")) for p in posts}
        staff = await resolve_staff_ids(client, authors, base=staff, cache=staff_cache)
    pairs = split_ticket_into_pairs(ticket_id, posts, staff)
    if not pairs:
        return 0
    issue_type = str(ticket.get("type_id") or "") or None
    try:
        info = await client.get_ticket_info(ticket_id)
        client_id = str(getattr(info, "client_id", "") or "") or None
    except Exception:
        client_id = None

    created_count = 0
    for pair in pairs:
        if pair["content_hash"] in known:
            continue
        emb_text = build_embedding_text(pair)
        embedding = await _embed_fn(emb_text, task_type="passage")
        emb_bytes = embedding.tobytes() if embedding is not None else None
        status = "ready" if emb_bytes is not None else "pending"
        _, created = await _save_fn(
            ticket_id=ticket_id,
            context=pair["context"],
            operator_answer=pair["operator_answer"],
            content_hash=pair["content_hash"],
            source_message_id=pair["source_message_id"],
            context_until_message_id=pair["context_until_message_id"],
            operator_message_id=pair["operator_message_id"],
            operator_user_id=pair.get("operator_user_id"),
            operator_answer_at=pair.get("operator_answer_at"),
            issue_type=issue_type,
            client_id=client_id,
            resolution_status="closed",
            embedding=emb_bytes,
            embedding_model=_EMBED_MODEL if emb_bytes is not None else None,
            embedding_status=status,
            embedding_text_hash=_embedding_text_hash(emb_text),
        )
        known.add(pair["content_hash"])
        if created:
            created_count += 1
    return created_count


async def reembed_pending(*, _embed_fn=None, _list_fn=None, _set_fn=None, limit=200) -> int:
    """Добирает эмбеддинги для pending/failed пар. Возвращает число ставших ready."""
    if _embed_fn is None:
        from ..knowledge.indexer import embed_text as _embed_fn
    if _list_fn is None:
        from ..db import list_pending_embeddings as _list_fn
    if _set_fn is None:
        from ..db import set_pair_embedding as _set_fn
    rows = await _list_fn(limit)
    fixed = 0
    for row in rows:
        pair = {"context": row["context"], "operator_answer": row["operator_answer"]}
        embedding = await _embed_fn(build_embedding_text(pair), task_type="passage")
        if embedding is None:
            await _set_fn(row["pair_id"], None, None, "failed")
            continue
        await _set_fn(row["pair_id"], embedding.tobytes(), _EMBED_MODEL, "ready")
        fixed += 1
    return fixed

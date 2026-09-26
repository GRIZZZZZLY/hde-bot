"""Помощники контекста агента v2 (spec 2026-09-27 §3, п.2).

Всё чистое и тестируемое. Причины, по которым это вообще понадобилось, — в
docs/agent-review-2026-09-27.md: поиск шёл по телефону или «Хорошо» (51%
черновиков), модель видела начало статьи вместо шагов, а макросы первой линии
в истории учили её отправлять клиента ждать специалиста.
"""
from __future__ import annotations

import re

from .operator_text import BOILERPLATE_RE, CLOSER_RE, clean_operator_text, strip_html

_WORD_RE = re.compile(r"[а-яёa-z]+", re.I)
_ACK_WORDS = frozenset({
    "хорошо", "спасибо", "ок", "окей", "ok", "да", "нет", "ждем", "ждём", "жду",
    "благодарю", "понял", "поняла", "понятно", "ясно", "сейчас", "минуту", "секунду",
    "отлично", "принято", "верно", "все", "всё", "так", "ани", "деск", "анидеск",
    "рудесктоп", "номер", "телефон", "вот", "пожалуйста",
})
_MIN_LETTERS = 12

_REMOTE_INSTRUCTION_RE = re.compile(
    r"(скачайте|установите|запустите|откройте)[^.\n]{0,60}"
    r"(anydesk|rudesktop|анидеск|рудесктоп|удал[её]нн\w+ доступ)",
    re.I,
)
_REMOTE_MARKER = "[ранее предложено удалённое подключение]"
_STAFF_PREFIX = "Сотрудник:"

_STRESS_RE = re.compile(
    r"опять|снова|вчера уже|до сих пор|который раз|очеред|закрыть смену|смену закрыть|"
    r"не (?:можем|могу) (?:продавать|пробить|работать)|клиенты ждут|стоим|срочно|"
    r"!!|где ваш|сколько можно|жду уже|никто не (?:отвечает|ответил)",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|приветствую)[!.,]*\s*", re.I
)
_STOP_WORDS = frozenset({"после", "перед", "когда", "почему", "подскажите", "пожалуйста"})


def is_substantive(text: str) -> bool:
    """Содержательное сообщение клиента: не телефон, не ID, не подтверждение."""
    words = _WORD_RE.findall((text or "").lower())
    if sum(len(w) for w in words) < _MIN_LETTERS:
        return False
    return not all(w in _ACK_WORDS for w in words)


def client_messages(posts, client_id) -> list[str]:
    ordered = sorted(posts, key=lambda p: int(getattr(p, "post_id", 0) or 0))
    return [
        strip_html(getattr(p, "text", ""))
        for p in ordered
        if not getattr(p, "is_comment", False)
        and str(getattr(p, "user_id", "")) == str(client_id)
    ]


def build_retrieval_query(title: str, messages: list[str]) -> str:
    useful = [m for m in messages if is_substantive(m)]
    parts = [title or ""]
    if useful:
        parts.append(useful[0])
        if useful[-1] != useful[0]:
            parts.append(useful[-1])
    return "\n".join(p for p in parts if p)[:600]


def strip_history_macros(history: str) -> str:
    """Штампы первой линии и бота — вон, инструкция по удалёнке — одной меткой."""
    kept: list[str] = []
    for line in (history or "").splitlines():
        if not line.startswith(_STAFF_PREFIX):
            kept.append(line)
            continue
        body = line[len(_STAFF_PREFIX):].strip()
        if BOILERPLATE_RE.search(body):
            continue
        if _REMOTE_INSTRUCTION_RE.search(body):
            kept.append(f"{_STAFF_PREFIX} {_REMOTE_MARKER}")
            continue
        cleaned = clean_operator_text(body)
        if cleaned:
            kept.append(f"{_STAFF_PREFIX} {cleaned}")
    return "\n".join(kept)


def _query_terms(query: str) -> set[str]:
    return {
        w[:5] for w in _WORD_RE.findall((query or "").lower())
        if len(w) >= 4 and w not in _STOP_WORDS
    }


def best_chunk(content: str, query: str, target: int = 700) -> str:
    """Кусок статьи с наибольшим числом слов запроса.

    ponytail: лексический выбор по основам слов (5 букв), без эмбеддингов на
    каждый кусок; если промахивается — эмбеддить куски при индексации.
    """
    from ..knowledge.chunker import chunk_markdown

    content = (content or "").strip()
    if len(content) <= target:
        return content
    chunks = chunk_markdown(content, target=target, overlap=100) or [content[:target]]
    terms = _query_terms(query)

    def score(chunk: str) -> int:
        stems = {w[:5] for w in _WORD_RE.findall(chunk.lower())}
        return len(terms & stems)

    best = max(chunks, key=score)          # max берёт первый при равенстве
    return best[: int(target * 1.5)]


def detect_stress(messages: list[str]) -> bool:
    return any(_STRESS_RE.search(m or "") for m in messages)


def is_first_staff_reply(posts, client_id) -> bool:
    """Сотрудник ещё ничего содержательного клиенту не писал (штампы не в счёт)."""
    for p in posts:
        if getattr(p, "is_comment", False):
            continue
        if str(getattr(p, "user_id", "")) == str(client_id):
            continue
        if clean_operator_text(strip_html(getattr(p, "text", ""))):
            return False
    return True


def clean_demo_answer(text: str) -> str:
    text = _GREETING_RE.sub("", strip_html(text or ""))
    text = CLOSER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()

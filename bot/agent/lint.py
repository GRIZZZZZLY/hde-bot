"""Проверки черновика кодом, без вызовов модели (spec 2026-09-27 §5).

Замена LLM self-check. Тот вешал ⚠️ на каждый второй черновик, пометки перестали
читать. Правило здесь: пометка в Памятке только там, где почти наверняка
ошибка (hard). Мелочи идут в soft — только в базу, для статистики.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .operator_text import CLOSER_RE
from .voice import ALLOWED_URLS

_PASSWORD_RE = re.compile(
    r"(парол\w*|password|pwd)((?:\s+[а-яёa-z]+){0,3}\s*[:\-–—]\s*|\s+)"
    r"(?!(?:и|от|для)\b)([A-Za-z0-9!@#$%^&*._\-]{4,})",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|приветствую)[!.,]*\s*", re.I
)
_PROMISE_RE = re.compile(
    r"\b(инженер|специалист|разработчик|мастер|техник)\w*"
    r"(?:[^\w.!?]+\w+){0,3}?[^\w.!?]+"
    r"(?:свяж|позвон|перезвон|подключ|приед|провер|ответ)(?!\w*ите\b)\w*",
    re.I,
)
_DEADLINE_RE = re.compile(
    r"\b(?:завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|"
    r"воскресенье)|с\s+\d{1,2}(?::\d{2})?\s*(?:утра|час\w*)|в\s+течени[ея]\s+\d+)",
    re.I,
)
_PAST_SELF_RE = re.compile(
    r"(?:^|[.!?]\s*|\bя\s+(?:\w+\s+){0,2})"
    r"(подключил(?:ся|ась)?|настроил(?:а)?|проверил(?:а)?|обновил(?:а)?|исправил(?:а)?|перезагрузил(?:а)?)\b",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s)»\"']+")
_NUMBER_RE = re.compile(r"\+?\d[\d\s()\-]{5,}\d|\b\d+\.\d+(?:\.\d+)*\b|\b\d{4,}\b")
_CONVEYOR_RE = re.compile(
    r"спасибо за обращение|приносим\s+(?:свои\s+)?извинения|в кратчайшие сроки|"
    r"на (?:текущий|данный) момент|данный вопрос|уважаем\w+\s+(?:клиент|пользовател)|"
    r"информируем вас|должна быть решена|надеюсь,? это поможет|"
    r"если (?:у вас )?(?:возникнут|появятся|есть) вопросы|для дальнейшей диагностики|"
    r"необходимо выполнить следующие",
    re.I,
)
_MAX_WORDS = 60


@dataclass
class LintResult:
    client: str
    memo: str
    fixed: list[str] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    def warning_line(self) -> str:
        return f"⚠️ Проверь: {self.hard[0]}" if self.hard else ""

    def as_dict(self) -> dict:
        return {"fixed": self.fixed, "hard": self.hard, "soft": self.soft}


def _staff_text(history: str) -> str:
    return "\n".join(
        ln for ln in (history or "").splitlines()
        if ln.startswith(("Сотрудник:", "Коллега:"))
    ).lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _norm_url(u: str) -> str:
    return u.lower().rstrip(".,;:!?/")


def _unknown_anchor(client: str, haystack: str) -> str | None:
    hay_urls = {_norm_url(u) for u in _URL_RE.findall(haystack)}
    hay_urls |= {_norm_url(u) for u in ALLOWED_URLS}
    for url in _URL_RE.findall(client):
        if _norm_url(url) not in hay_urls:
            return url
    without_urls = _URL_RE.sub(" ", client)
    hay_numbers = [_digits(n) for n in _NUMBER_RE.findall(_URL_RE.sub(" ", haystack))]
    for raw in _NUMBER_RE.findall(without_urls):
        d = _digits(raw)
        if d and not any(d in h for h in hay_numbers if h):
            return raw.strip()
    return None


def check_draft(
    client: str,
    memo: str,
    *,
    history: str,
    sources_text: str,
    facts: str = "",
    first_staff_reply: bool = False,
    grounds: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> LintResult:
    res = LintResult(client=(client or "").strip(), memo=(memo or "").strip())

    # fix: пароль в Памятке
    masked = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", res.memo)
    if masked != res.memo:
        res.memo = masked
        res.fixed.append("password")

    # fix: приветствие не к месту и дежурная концовка
    if not first_staff_reply:
        stripped = _GREETING_RE.sub("", res.client)
        if stripped != res.client:
            res.client = stripped
            res.fixed.append("greeting")
    closed = re.sub(r"\s+", " ", CLOSER_RE.sub("", res.client)).strip()
    if closed != res.client:
        res.client = closed
        res.fixed.append("closer")

    text = res.client
    low = text.lower()
    staff = _staff_text(history)

    m = _PROMISE_RE.search(text)
    if m and m.group(1).lower()[:6] not in staff:
        res.hard.append(f"обещание за других («{m.group(0)}») — в истории его нет")

    d = _DEADLINE_RE.search(text)
    grounding = "\n".join([history or "", sources_text or "", facts or ""]).lower()
    if d and d.group(0).lower() not in grounding:
        res.hard.append(f"срок «{d.group(0)}» — в истории его нет")

    anchor = _unknown_anchor(text, "\n".join([history or "", sources_text or "", facts or ""]))
    if anchor:
        res.hard.append(f"цифры или ссылка «{anchor}» не из переписки и не из источников")

    p = _PAST_SELF_RE.search(text)
    if p:
        res.hard.append(f"прошедшее время о несделанном («{p.group(1)}»)")

    # модель иногда пишет метку в скобках, как в промпте: «[KB#12]»
    known = set(grounds or [])
    unknown = [s for s in (x.strip().strip("[]") for x in (source_ids or [])) if s not in known]
    if unknown:
        res.hard.append(f"источник {unknown[0]} не находили")

    if _CONVEYOR_RE.search(low):
        res.soft.append("conveyor")
    if text.count("?") > 1:
        res.soft.append("questions")
    if len(text.split()) > _MAX_WORDS:
        res.soft.append("length")
    return res

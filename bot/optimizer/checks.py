"""Hard-checks кандидата: то, что проверяется кодом, а не судьёй (спека §11.2).

Отдавать LLM условие, которое проверяется регекспом («есть ли секция
«Клиенту:»», «сколько слов»), значит платить токенами за шум и получать
субъективную оценку факта. Судье остаётся только то, что требует смысла
(см. judge.AXES).

Разделение реакций тоже не косметическое:

- структурный провал, пустой ответ, протёкший reasoning и safety-категория в
  клиентской части **обнуляют кейс**: такой ответ нельзя отправить, и
  усреднять его с осмысленными оценками бессмысленно;
- длина — **штраф**, а не ноль. Лимит зависит от действия (ASK короче ANSWER),
  а действие, которое выберет кандидат, заранее неизвестно: `action_type` из
  сэмпла — это действие, выбранное ботом на том же тикете, то есть ожидание,
  а не факт. Обнулять кейс по ожиданию нельзя.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Лимит слов в «Клиенту» по действию. Один лимит на всё — неверно: уточняющий
# вопрос обязан быть короче готового решения, а эскалация короче обоих.
WORD_LIMITS: dict[str, int] = {
    "ANSWER": 24,
    "ASK": 18,
    "ESCALATE": 12,
    "NO_ACTION": 12,
}
DEFAULT_WORD_LIMIT = 24
LENGTH_PENALTY = 0.8

_CLIENT_SECTION_RE = re.compile(
    r"(?is)клиенту\s*:(.*?)(?:\n\s*(?:памятк|суть)\w*\s*:|$)"
)
_REASONING_RE = re.compile(r"(?is)<(?:reasoning|think)>.*?</(?:reasoning|think)>")
_REASONING_TAG_RE = re.compile(r"(?i)<\s*/?\s*(?:reasoning|think)\b")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def client_facing_text(generated: str) -> str:
    """Часть ответа, адресованная клиенту, без reasoning-блоков.

    Операторские секции (Суть/Памятка) отрезаются: упоминание фискального или
    финансового действия там — это ДИАГНОЗ оператору, а не инструкция клиенту,
    и safety-скан по ним даёт ложные срабатывания.
    """
    text = _REASONING_RE.sub("", generated or "")
    match = _CLIENT_SECTION_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _raw_client_section(generated: str) -> str:
    """То же, но БЕЗ вырезания reasoning — чтобы увидеть протёкшие теги."""
    match = _CLIENT_SECTION_RE.search(generated or "")
    return match.group(1) if match else (generated or "")


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def word_limit(action_type: str | None) -> int:
    return WORD_LIMITS.get((action_type or "").upper(), DEFAULT_WORD_LIMIT)


@dataclass
class CheckResult:
    """fatal — причина обнуления кейса (или None); penalty — множитель в (0, 1]."""

    fatal: str | None = None
    penalty: float = 1.0
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.fatal is not None


def hard_check(
    generated: str, *, action_type: str | None = None, _safety_fn=None
) -> CheckResult:
    """Детерминированная часть оценки. Судью вызывать только если fatal is None."""
    if _safety_fn is None:
        from ..agent.safety import post_generation_safety_check as _safety_fn

    text = generated or ""
    if not text.strip():
        return CheckResult(fatal="empty", detail="пустой ответ")

    if not _CLIENT_SECTION_RE.search(text):
        return CheckResult(
            fatal="no_client_section", detail="нет секции «Клиенту:»"
        )

    if _REASONING_TAG_RE.search(_raw_client_section(text)):
        return CheckResult(
            fatal="reasoning_leak", detail="reasoning протёк в текст клиенту"
        )

    client = client_facing_text(text)
    if not client:
        return CheckResult(fatal="empty", detail="секция «Клиенту» пуста")

    decision = _safety_fn(client)
    if decision.action == "ESCALATE":
        return CheckResult(
            fatal="safety",
            detail=f"safety-категория в тексте клиенту: {decision.category}",
        )

    limit = word_limit(action_type)
    words = word_count(client)
    if words > limit:
        return CheckResult(
            penalty=LENGTH_PENALTY,
            detail=f"«Клиенту» {words} слов при лимите {limit} ({action_type or 'ANSWER'})",
        )
    return CheckResult()

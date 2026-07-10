"""Rule-based safety gate. The ESCALATE decision is made by CODE, not by a
prompt instruction — that is what makes it a hard gate (spec Phase 0).

Only actions-with-consequences escalate; explanation/diagnosis proceeds.
The rule lists are a starter set, tuned as real cases arrive."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PolicyDecision:
    action: str            # "PROCEED" | "ESCALATE"
    category: str | None   # matched escalation category, or None
    matched: str | None    # matched pattern, or None


# (category, [regex patterns]) — matched case-insensitively against lowered text.
_ESCALATION_RULES: list[tuple[str, list[str]]] = [
    ("finance", [
        r"верн\w*\s+деньг", r"возврат\w*\s+средств", r"сдела\w*\s+возврат",
        r"измен\w*\s+тариф", r"пересчита\w*\s+оплат",
    ]),
    ("fiscal_change", [
        r"перерегистр\w*\s+(?:ккт|касс|фн)", r"смен\w*\s+офд", r"замен\w*\s+фн",
    ]),
    ("data_loss", [
        r"удали\w*\s+(?:все\s+)?(?:данные|базу|товары|продажи)",
        r"сброс\w*\s+до\s+заводск", r"переустанов\w*\b.*потер",
    ]),
    ("access", [
        r"смен\w*\s+парол", r"выда\w*\s+доступ", r"перенос\w*\s+аккаунт",
        r"восстанов\w*\s+доступ",
    ]),
]


def pre_generation_policy_check(text: str) -> PolicyDecision:
    """Scan the incoming request. Escalation-category intent → ESCALATE."""
    low = (text or "").lower()
    for category, patterns in _ESCALATION_RULES:
        for pat in patterns:
            if re.search(pat, low):
                return PolicyDecision(action="ESCALATE", category=category, matched=pat)
    return PolicyDecision(action="PROCEED", category=None, matched=None)


def post_generation_safety_check(answer_text: str) -> PolicyDecision:
    """Second pass over the MODEL'S answer: if the generated text itself proposes
    an escalation-category action, force ESCALATE regardless of chosen action_type."""
    return pre_generation_policy_check(answer_text)

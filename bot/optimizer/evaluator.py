"""Evaluate prompt candidates by replaying historical tickets."""
from __future__ import annotations

import difflib
import logging
import random
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_OUTCOME_WEIGHTS = {
    "sent": 1.0,
    "accepted": 1.0,
    "corrected": 0.4,
    "rejected": 0.0,
}

# Max samples to replay (limits API cost: 20 samples × 3 candidates = 60 Gemini calls)
_MAX_EVAL_SAMPLES = 20

GenerateFn = Callable[[str, str, str], Awaitable[str]]


async def combined_score(
    samples: list[dict],
    format_instructions: str,
    *,
    _generate_fn: GenerateFn | None = None,
) -> float:
    """Evaluate format_instructions on a sample set. Returns score in [0, 1].

    Args:
        samples: list of dicts from get_optimization_samples()
        format_instructions: candidate FORMAT_INSTRUCTIONS text to evaluate
        _generate_fn: injectable for testing; defaults to _generate_answer
    """
    if not samples:
        return 0.0

    generate = _generate_fn or _generate_answer

    # Limit to avoid excessive API cost
    eval_set = samples if len(samples) <= _MAX_EVAL_SAMPLES else random.sample(samples, _MAX_EVAL_SAMPLES)

    acceptance_scores: list[float] = []
    similarity_scores: list[float] = []

    for sample in eval_set:
        try:
            generated = await generate(
                sample.get("history", ""),
                sample.get("title", ""),
                format_instructions,
            )
        except Exception as exc:
            logger.warning("Evaluator generate failed for sample %s: %s", sample.get("ticket_id"), exc)
            continue

        outcome = sample.get("outcome", "rejected")
        weight = _OUTCOME_WEIGHTS.get(outcome, 0.0)

        # Acceptance: does generated answer look like what operator approved?
        op_answer = sample.get("op_answer") or sample.get("ai_answer", "")
        ratio = difflib.SequenceMatcher(
            None, generated.lower(), op_answer.lower()
        ).ratio() if op_answer else 0.0
        accepted = 1.0 if ratio >= 0.65 else 0.0
        acceptance_scores.append(accepted * weight)

        # Similarity (only for 'corrected' — op_answer is the ground truth)
        if outcome == "corrected" and sample.get("op_answer"):
            sim = difflib.SequenceMatcher(
                None, generated.lower(), sample["op_answer"].lower()
            ).ratio()
            similarity_scores.append(sim)

    if not acceptance_scores:
        return 0.0

    acceptance = sum(acceptance_scores) / len(acceptance_scores)
    if similarity_scores:
        similarity = sum(similarity_scores) / len(similarity_scores)
        return 0.7 * acceptance + 0.3 * similarity
    # No corrected samples — use acceptance alone as the full score
    return acceptance


async def _generate_answer(history: str, title: str, format_instructions: str) -> str:
    """Call Gemini with a custom format_instructions. Returns generated text."""
    import aiohttp
    from ..config import config

    system = (
        f"Ты AI-ассистент специалиста 2-й линии поддержки кассового оборудования.\n\n"
        f"{format_instructions}"
    )
    user = f"Тема тикета: {title}\n\n{history[-2000:]}"

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            params={"key": config.gemini_api_key},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Extract only the "Ответ:" line if present
    for line in text.splitlines():
        if line.lower().startswith("ответ:"):
            return line[len("ответ:"):].strip()
    return text

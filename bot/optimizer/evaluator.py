"""Evaluate prompt candidates by replaying historical tickets."""
from __future__ import annotations

import difflib
import logging
import random
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_OUTCOME_WEIGHTS = {
    "sent": 1.0,
    "accepted": 0.8,
    "corrected": 0.4,
    "rejected": 0.0,
}

# Max samples to replay (limits API cost: 20 samples × 3 candidates = 60 LLM calls)
_MAX_EVAL_SAMPLES = 20

# Anti-degradation thresholds — penalise "lazy" short/structurally broken answers
_MIN_LENGTH_RATIO = 0.3           # generated must be ≥30% of reference length
_LENGTH_PENALTY = 0.5             # multiplier when length gate fails
_MIN_JACCARD = 0.3                # word-set overlap with reference
_JACCARD_PENALTY = 0.7            # multiplier when jaccard gate fails
_CLIENT_SECTION_RE = re.compile(r"клиенту\s*:", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)

GenerateFn = Callable[[str, str, str], Awaitable[str]]


def _word_set(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 2}


def _quality_multiplier(generated: str, ref_text: str) -> float:
    """Return multiplier in [0, 1] penalising lazy/broken answers.

    Zero if structural check fails (no "Клиенту:" section).
    Otherwise combines length-gate and jaccard-gate penalties.
    """
    if not _CLIENT_SECTION_RE.search(generated):
        return 0.0

    mult = 1.0
    if ref_text and len(generated) < _MIN_LENGTH_RATIO * len(ref_text):
        mult *= _LENGTH_PENALTY

    ref_words = _word_set(ref_text)
    if ref_words:
        gen_words = _word_set(generated)
        overlap = len(gen_words & ref_words) / len(ref_words)
        if overlap < _MIN_JACCARD:
            mult *= _JACCARD_PENALTY
    return mult


async def combined_score(
    samples: list[dict],
    format_instructions: str,
    *,
    max_samples: int | None = _MAX_EVAL_SAMPLES,
    _generate_fn: GenerateFn | None = None,
) -> float:
    """Evaluate format_instructions on a sample set. Returns score in [0, 1].

    Args:
        samples: list of dicts from get_optimization_samples()
        format_instructions: candidate FORMAT_INSTRUCTIONS text to evaluate
        max_samples: обрезка выборки для экономии API; None = оценивать все детерминированно.
        _generate_fn: injectable for testing; defaults to _generate_answer
    """
    if not samples:
        return 0.0

    generate = _generate_fn or _generate_answer

    # Limit to avoid excessive API cost. Fixed seed: baseline and all
    # candidates must be measured on the SAME subsample, otherwise their
    # scores are not comparable.
    if max_samples is not None and len(samples) > max_samples:
        eval_set = random.Random(42).sample(samples, max_samples)
    else:
        eval_set = samples

    acceptance_scores: list[float] = []
    similarity_scores: list[float] = []

    for sample in eval_set:
        outcome = sample.get("outcome", "rejected")
        weight = _OUTCOME_WEIGHTS.get(outcome, 0.0)
        ref_text = sample.get("op_answer") or sample.get("ai_answer", "")

        if not ref_text:
            # No reference text to compare against — trust the operator's signal directly.
            # accepted/sent samples were approved, rejected were not. No generation needed.
            direct = 1.0 if outcome in ("accepted", "sent") else 0.0
            acceptance_scores.append(direct * weight)
            continue

        # Has reference text — generate and measure similarity
        try:
            generated = await generate(
                sample.get("history", ""),
                sample.get("title", ""),
                format_instructions,
            )
        except Exception as exc:
            logger.warning("Evaluator generate failed for sample %s: %s", sample.get("ticket_id"), exc)
            continue

        # Acceptance: does generated answer resemble what operator approved/sent?
        ratio = difflib.SequenceMatcher(
            None, generated.lower(), ref_text.lower()
        ).ratio()
        accepted = 1.0 if ratio >= 0.65 else 0.0
        quality = _quality_multiplier(generated, ref_text)
        acceptance_scores.append(accepted * weight * quality)

        # Similarity ground-truth (only for 'corrected' — op_answer is what operator wrote)
        if outcome == "corrected" and sample.get("op_answer"):
            sim = difflib.SequenceMatcher(
                None, generated.lower(), sample["op_answer"].lower()
            ).ratio()
            similarity_scores.append(sim * quality)

    if not acceptance_scores:
        return 0.0

    acceptance = sum(acceptance_scores) / len(acceptance_scores)
    if similarity_scores:
        similarity = sum(similarity_scores) / len(similarity_scores)
        return 0.7 * acceptance + 0.3 * similarity
    # No corrected samples — use acceptance alone as the full score
    return acceptance


async def _generate_answer(history: str, title: str, format_instructions: str) -> str:
    """Call Groq llama-3.3 (same model as prod summaries) with custom format_instructions."""
    import aiohttp
    from ..config import config

    system = (
        f"Ты AI-ассистент специалиста 2-й линии поддержки кассового оборудования.\n\n"
        f"{format_instructions}"
    )
    user = f"Тема тикета: {title}\n\n{history[-2000:]}"

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    url = "https://api.groq.com/openai/v1/chat/completions"
    from ..llm_semaphore import LLM_SEMAPHORE  # local import: avoid cycle
    async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {config.groq_api_key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()

    choices = data.get("choices")
    if not choices:
        error = data.get("error", {})
        msg = error.get("message") if isinstance(error, dict) else str(data)
        raise RuntimeError(f"Groq returned no choices: {msg}")
    text = choices[0]["message"]["content"].strip()
    # Extract only the "Ответ:" line if present
    for line in text.splitlines():
        if line.lower().startswith("ответ:"):
            return line[len("ответ:"):].strip()
    return text

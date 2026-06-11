"""LLM-as-judge: оценка ответа кандидата против реального ответа оператора.

Используется офлайн-харнессом (scripts/eval_prompt.py) и ночным
оптимизатором (agent.py) для финальной оценки на holdout-наборе.
Gemini 2.5 Flash, temperature=0, structured output (responseSchema).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from ..llm_semaphore import LLM_SEMAPHORE
from .evaluator import combined_score, _generate_answer

logger = logging.getLogger(__name__)

_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "diagnosis_correct": {"type": "BOOLEAN"},
        "equipment_named": {"type": "BOOLEAN"},
        "format_ok": {"type": "BOOLEAN"},
        "length_ok": {"type": "BOOLEAN"},
        "sounds_human": {"type": "BOOLEAN"},
        "overall": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
    },
    "required": [
        "diagnosis_correct", "equipment_named", "format_ok",
        "length_ok", "sounds_human", "overall", "reason",
    ],
}

CallFn = Callable[[str, str], Awaitable[str]]
JudgeFn = Callable[[str, str, str, str], Awaitable[dict | None]]
GenerateFn = Callable[[str, str, str], Awaitable[str]]


def _load_style_guide() -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / "style_guide_ru.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_judge_prompt(
    history: str, title: str, candidate: str, op_answer: str
) -> tuple[str, str]:
    """Returns (system, user) для запроса к судье."""
    system = (
        "Ты строгий судья качества ответов AI-ассистента поддержки кассового "
        "оборудования (АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).\n"
        "Ответ ассистента имеет формат «Суть / Клиенту / Памятка».\n\n"
        "Сравни ответ кандидата с реальным ответом оператора и оцени:\n"
        "- diagnosis_correct: верно ли понята причина проблемы (сверяй с оператором)\n"
        "- equipment_named: названа ли модель оборудования, если она есть в истории\n"
        "- format_ok: есть ли секция «Клиенту:», нет ли лишних секций\n"
        "- length_ok: поле «Клиенту» не длиннее ~20 слов, без воды\n"
        "- sounds_human: текст для клиента звучит как живой оператор, "
        "БЕЗ перечисленных ниже ИИ-паттернов\n"
        "- overall: целое 0-10, общая оценка\n"
        "- reason: одна фраза, что главное не так (или «ок»)\n\n"
        "Стайлгайд для sounds_human:\n"
        f"{_load_style_guide()}"
    )
    user = (
        f"Тема тикета: {title}\n\n"
        f"История (фрагмент):\n{history[-2000:]}\n\n"
        f"Ответ кандидата:\n{candidate}\n\n"
        f"Реальный ответ оператора:\n{op_answer}"
    )
    return system, user


async def _call_gemini(system: str, user: str) -> str:
    from ..config import config

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 500,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }
    async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
        async with session.post(
            _URL,
            params={"key": config.gemini_api_key},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
    candidates = data.get("candidates")
    if not candidates:
        error = data.get("error", {})
        msg = error.get("message") if isinstance(error, dict) else str(data)
        raise RuntimeError(f"Gemini judge returned no candidates: {msg}")
    return candidates[0]["content"]["parts"][0]["text"].strip()


async def judge_answer(
    history: str,
    title: str,
    candidate: str,
    op_answer: str,
    *,
    _call_fn: CallFn | None = None,
) -> dict | None:
    """Вердикт судьи или None (после одного повтора при битом JSON/ошибке)."""
    call = _call_fn or _call_gemini
    system, user = build_judge_prompt(history, title, candidate, op_answer)
    for attempt in (1, 2):
        try:
            raw = await call(system, user)
            verdict = json.loads(raw)
            if not isinstance(verdict.get("overall"), int):
                raise ValueError("overall is not int")
            return verdict
        except Exception as exc:
            logger.warning("Judge attempt %d failed: %s", attempt, exc)
    return None


async def holdout_score(
    samples: list[dict],
    format_instructions: str,
    *,
    _generate_fn: GenerateFn | None = None,
    _judge_fn: JudgeFn | None = None,
) -> float:
    """Финальный скор на holdout: 0.5 * similarity_base + 0.5 * judge.

    Генерация мемоизируется, чтобы base и судья не дёргали Gemini дважды
    за один сэмпл.

    base = среднее SequenceMatcher-сходство generated vs op_answer по
    сэмплам, у которых op_answer есть; по остальным — combined_score.

    Сэмплы без op_answer судьёй не оцениваются; если таких нет вовсе —
    возвращается чистый base.
    """
    import difflib

    raw_generate = _generate_fn or _generate_answer
    judge = _judge_fn or judge_answer

    memo: dict[tuple[str, str], str] = {}

    async def generate(history: str, title: str, instructions: str) -> str:
        key = (history, title)
        if key not in memo:
            memo[key] = await raw_generate(history, title, instructions)
        return memo[key]

    # Split samples into those with and without op_answer
    with_ref = [s for s in samples if s.get("op_answer")]
    without_ref = [s for s in samples if not s.get("op_answer")]

    # Base score: similarity vs op_answer for samples that have one
    base_scores: list[float] = []
    judge_scores: list[float] = []

    for s in with_ref:
        ref = s["op_answer"]
        try:
            answer = await generate(s.get("history", ""), s.get("title", ""), format_instructions)
        except Exception as exc:
            logger.warning("Generate failed for sample %s: %s", s.get("ticket_id"), exc)
            continue

        sim = difflib.SequenceMatcher(None, answer.lower(), ref.lower()).ratio()
        base_scores.append(sim)

        try:
            verdict = await judge(s.get("history", ""), s.get("title", ""), answer, ref)
        except Exception as exc:
            logger.warning("Judge failed for sample %s: %s", s.get("ticket_id"), exc)
            verdict = None
        if verdict is not None:
            judge_scores.append(min(max(verdict["overall"], 0), 10) / 10.0)

    # For samples without op_answer, fall back to combined_score contribution
    if without_ref:
        fallback = await combined_score(
            without_ref, format_instructions, max_samples=None, _generate_fn=generate
        )
        # Weight fallback by proportion
        n_with = len(base_scores)
        n_without = len(without_ref)
        total = n_with + n_without
        if n_with > 0:
            base = (sum(base_scores) * n_with / total) + (fallback * n_without / total)
        else:
            base = fallback
    else:
        base = sum(base_scores) / len(base_scores) if base_scores else 0.0

    if not judge_scores:
        return base
    return 0.5 * base + 0.5 * (sum(judge_scores) / len(judge_scores))

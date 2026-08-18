"""LLM-as-judge: оценка ответа кандидата против реального ответа оператора.

Используется офлайн-харнессом (scripts/eval_prompt.py) и оптимизатором
(agent.py, запуск ручной через /aioptimize) для финальной оценки на
holdout-наборе. Модель — OPTIMIZER_JUDGE_MODEL: обязана быть другого
семейства, чем генератор, иначе судья поощряет собственный стиль
(self-preference). temperature=0, JSON-режим.
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

_URL = "https://api.groq.com/openai/v1/chat/completions"

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
        "Верни ТОЛЬКО JSON-объект с полями diagnosis_correct, equipment_named, "
        "format_ok, length_ok, sounds_human (булевы), overall (целое 0-10), "
        "reason (строка). Без пояснений вне JSON.\n\n"
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


async def _call_groq_judge(system: str, user: str) -> str:
    from ..config import config

    payload = {
        "model": config.optimizer_judge_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
        async with session.post(
            _URL,
            headers={"Authorization": f"Bearer {config.groq_api_key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
    choices = data.get("choices")
    if not choices:
        error = data.get("error", {})
        msg = error.get("message") if isinstance(error, dict) else str(data)
        raise RuntimeError(f"Groq judge returned no choices: {msg}")
    return choices[0]["message"]["content"].strip()


async def judge_answer(
    history: str,
    title: str,
    candidate: str,
    op_answer: str,
    *,
    _call_fn: CallFn | None = None,
) -> dict | None:
    """Вердикт судьи или None (после одного повтора при битом JSON/ошибке)."""
    call = _call_fn or _call_groq_judge
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
    """Финальный скор на holdout: 0.5 * combined_score + 0.5 * judge.

    Генерация мемоизируется, чтобы combined_score и судья не дёргали
    LLM дважды за один сэмпл. Сэмплы без op_answer судьёй не
    оцениваются; если таких нет вовсе — возвращается чистый combined_score.
    """
    raw_generate = _generate_fn or _generate_answer
    judge = _judge_fn or judge_answer

    memo: dict[tuple[str, str], str] = {}

    async def generate(history: str, title: str, instructions: str) -> str:
        key = (history, title)
        if key not in memo:
            memo[key] = await raw_generate(history, title, instructions)
        return memo[key]

    base = await combined_score(
        samples, format_instructions, max_samples=None, _generate_fn=generate
    )

    judge_scores: list[float] = []
    for s in samples:
        ref = s.get("op_answer")
        if not ref:
            continue
        try:
            answer = await generate(s.get("history", ""), s.get("title", ""), format_instructions)
            verdict = await judge(s.get("history", ""), s.get("title", ""), answer, ref)
        except Exception as exc:
            logger.warning("Judge failed for sample %s: %s", s.get("ticket_id"), exc)
            continue
        if verdict is not None:
            judge_scores.append(min(max(verdict["overall"], 0), 10) / 10.0)

    if not judge_scores:
        return base
    return 0.5 * base + 0.5 * (sum(judge_scores) / len(judge_scores))

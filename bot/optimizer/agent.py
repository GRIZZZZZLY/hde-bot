"""Nightly prompt optimization agent."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import db
from ..ai_summary import get_active_format_instructions, invalidate_prompt_cache
from ..config import config
from .evaluator import combined_score
from .llm_router import LLMRouter
from .mutations import build_mutation_prompt

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 10
_MIN_IMPROVEMENT = 0.03


async def run_optimizer(bot: Bot) -> None:
    """Main nightly loop. Called from scheduler."""
    logger.info("Prompt optimizer: starting")

    samples = await db.get_optimization_samples(days=30)
    if len(samples) < _MIN_SAMPLES:
        logger.info(
            "Prompt optimizer: not enough samples (%d < %d), skipping",
            len(samples), _MIN_SAMPLES,
        )
        return

    current_instructions = await get_active_format_instructions()

    try:
        baseline = await combined_score(samples, current_instructions)
    except Exception as exc:
        logger.warning("Optimizer: baseline evaluation failed: %s", exc)
        return
    logger.info("Optimizer: baseline score=%.3f on %d samples", baseline, len(samples))

    good = [s for s in samples if s["outcome"] in ("accepted", "sent")]
    bad = [s for s in samples if s["outcome"] in ("rejected", "corrected")]
    system_prompt, user_prompt = build_mutation_prompt(current_instructions, good, bad)

    router = LLMRouter(
        gemini_api_key=config.gemini_api_key,
        groq_api_key=config.groq_api_key,
    )
    try:
        mutations = await router.complete_all(system=system_prompt, user=user_prompt)
    except Exception as exc:
        logger.warning("Optimizer: mutation requests failed: %s", exc)
        return

    if not mutations:
        logger.warning("Optimizer: no mutations returned from any model")
        return

    scores: dict[str, tuple[str, float]] = {}
    for model_name, content in mutations.items():
        if not content or len(content) < 20:
            continue
        try:
            score = await combined_score(samples, content)
            scores[model_name] = (content, score)
            logger.info("Optimizer: %s score=%.3f", model_name, score)
        except Exception as exc:
            logger.warning("Optimizer: evaluation failed for %s: %s", model_name, exc)

    if not scores:
        logger.warning("Optimizer: all evaluations failed")
        return

    winner_model = max(scores, key=lambda m: scores[m][1])
    winner_content, winner_score = scores[winner_model]

    version_ids: dict[str, int] = {}
    for model_name, (content, score) in scores.items():
        vid = await db.save_prompt_version(content=content, score=score, proposed_by=model_name)
        version_ids[model_name] = vid

    if winner_score < baseline + _MIN_IMPROVEMENT:
        logger.info(
            "Optimizer: winner %.3f does not beat baseline %.3f + threshold %.2f, no report",
            winner_score, baseline, _MIN_IMPROVEMENT,
        )
        await db.reject_all_prompt_candidates()
        return

    winner_vid = version_ids[winner_model]
    await _send_report(
        bot=bot,
        winner_model=winner_model,
        winner_content=winner_content,
        winner_score=winner_score,
        winner_vid=winner_vid,
        baseline=baseline,
        current_instructions=current_instructions,
        sample_count=len(samples),
        all_scores=scores,
    )


async def _send_report(
    bot: Bot,
    winner_model: str,
    winner_content: str,
    winner_score: float,
    winner_vid: int,
    baseline: float,
    current_instructions: str,
    sample_count: int,
    all_scores: dict[str, tuple[str, float]],
) -> None:
    """Send Telegram report to operator with Apply/Reject/Detail buttons."""
    improvement_pct = round((winner_score - baseline) / max(baseline, 0.01) * 100)
    baseline_int = round(baseline * 100)
    winner_int = round(winner_score * 100)

    old_snippet = current_instructions[:150].replace("\n", " ")
    new_snippet = winner_content[:150].replace("\n", " ")

    text = (
        "\U0001f9ea <b>Ночная оптимизация промпта</b>\n\n"
        f"\U0001f4ca Данные: {sample_count} тикетов · 30 дней\n"
        f"\u26a1 Сейчас: {baseline_int} баллов\n\n"
        f"\U0001f947 Победитель: <b>{escape(winner_model)}</b>\n"
        f"\U0001f4c8 Результат: {winner_int} баллов (+{improvement_pct}%)\n\n"
        f"\U0001f4dd <b>Предложенное изменение:</b>\n"
        f"— было: <i>«{escape(old_snippet)}...»</i>\n"
        f"+ стало: <i>«{escape(new_snippet)}...»</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="\u2705 Применить", callback_data=f"opt:apply:{winner_vid}")
    builder.button(text="\u274c Отклонить", callback_data="opt:reject")
    builder.button(text="\U0001f4ca Подробнее", callback_data=f"opt:detail:{winner_vid}")
    builder.adjust(2, 1)

    await bot.send_message(
        chat_id=config.personal_chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    logger.info("Optimizer: report sent, winner=%s score=%.3f", winner_model, winner_score)

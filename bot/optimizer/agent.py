"""Nightly prompt optimization agent."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import db
from ..ai_summary import get_active_format_instructions, invalidate_prompt_cache
from ..config import config
from .dataset import split_samples
from .evaluator import combined_score
from .judge import holdout_score
from .llm_router import LLMRouter
from .mutations import build_mutation_prompt

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 10
_MIN_IMPROVEMENT = 0.03


def _progress_bar(pct: int) -> str:
    """Return a 10-block progress bar string for the given percentage (0-100)."""
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled) + f" {pct}%"


async def _update_progress(msg: Message, pct: int, status: str) -> None:
    """Edit the progress message in place. Silently ignore edit errors."""
    try:
        await msg.edit_text(
            f"⚙️ <b>Оптимизация промпта</b>\n\n"
            f"{_progress_bar(pct)}\n\n"
            f"{status}",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def run_optimizer(bot: Bot) -> None:
    """Main nightly loop. Called from scheduler."""
    logger.info("Prompt optimizer: starting")

    # Send start notification with progress bar
    progress_msg: Message | None = None
    try:
        progress_msg = await bot.send_message(
            chat_id=config.personal_chat_id,
            text=(
                f"⚙️ <b>Оптимизация промпта</b>\n\n"
                f"{_progress_bar(0)}\n\n"
                f"Загружаю данные..."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Optimizer: could not send start notification: %s", exc)

    samples = await db.get_optimization_samples(days=30)
    if len(samples) < _MIN_SAMPLES:
        logger.info(
            "Prompt optimizer: not enough samples (%d < %d), skipping",
            len(samples), _MIN_SAMPLES,
        )
        if progress_msg:
            try:
                await progress_msg.edit_text(
                    f"⚙️ <b>Оптимизация промпта</b>\n\n"
                    f"{_progress_bar(100)}\n\n"
                    f"⏭ Пропущено — недостаточно данных ({len(samples)} из {_MIN_SAMPLES} нужных).",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    train, holdout = split_samples(samples)
    if not holdout or not train:
        # слишком мало данных для честного сплита — оцениваем на всём
        train, holdout = samples, samples
        logger.warning(
            "Optimizer: degenerate split (%d samples), train == holdout — "
            "защита от переобучения ослаблена",
            len(samples),
        )

    if progress_msg:
        await _update_progress(progress_msg, 10, f"Данные загружены: {len(samples)} тикетов за 30 дней.\nСчитаю базовый скор...")

    current_instructions = await get_active_format_instructions()

    try:
        baseline = await combined_score(train, current_instructions)
    except Exception as exc:
        logger.warning("Optimizer: baseline evaluation failed: %s", exc)
        if progress_msg:
            await _update_progress(progress_msg, 10, f"❌ Ошибка при расчёте базового скора: {exc}")
        return
    logger.info("Optimizer: baseline score=%.3f on %d samples", baseline, len(train))

    if progress_msg:
        await _update_progress(progress_msg, 20, f"Базовый скор: {round(baseline * 100)} баллов.\nЗапрашиваю мутации у LLM...")

    good = [s for s in train if s["outcome"] in ("accepted", "sent")]
    bad = [s for s in train if s["outcome"] in ("rejected", "corrected")]
    system_prompt, user_prompt = build_mutation_prompt(current_instructions, good, bad)

    router = LLMRouter(
        groq_api_key=config.groq_api_key,
        openrouter_api_key=config.openrouter_api_key,
    )
    try:
        mutations, mut_errors = await router.complete_all(system=system_prompt, user=user_prompt)
    except Exception as exc:
        logger.warning("Optimizer: mutation requests failed: %s", exc)
        if progress_msg:
            await _update_progress(progress_msg, 20, f"❌ Ошибка при запросе мутаций: {exc}")
        return

    if not mutations:
        logger.warning("Optimizer: no mutations returned from any model, errors: %s", mut_errors)
        if progress_msg:
            err_lines = "\n".join(f"• {m}: {e}" for m, e in mut_errors.items()) or "нет деталей"
            await _update_progress(
                progress_msg, 50,
                f"❌ Ни одна модель не вернула мутацию.\n\n{err_lines}",
            )
        return

    if progress_msg:
        await _update_progress(
            progress_msg, 50,
            f"Получено мутаций: {len(mutations)} ({', '.join(mutations.keys())}).\nОцениваю кандидатов...",
        )

    scores: dict[str, tuple[str, float]] = {}
    for i, (model_name, content) in enumerate(mutations.items()):
        if not content or len(content) < 20:
            continue
        try:
            score = await combined_score(train, content)
            scores[model_name] = (content, score)
            logger.info("Optimizer: %s score=%.3f", model_name, score)
            pct = 50 + round((i + 1) / len(mutations) * 25)
            if progress_msg:
                await _update_progress(
                    progress_msg, pct,
                    f"Оценка {i + 1}/{len(mutations)}: {model_name} → {round(score * 100)} баллов.",
                )
        except Exception as exc:
            logger.warning("Optimizer: evaluation failed for %s: %s", model_name, exc)

    if not scores:
        logger.warning("Optimizer: all evaluations failed")
        if progress_msg:
            await _update_progress(progress_msg, 75, "❌ Все оценки завершились ошибкой.")
        return

    if progress_msg:
        await _update_progress(progress_msg, 78, "Финальная проверка на holdout-наборе...")

    # Holdout-гейт: топ-2 кандидата по train-скору пересчитываются судьёй
    # на отложенных сэмплах; apply-решение — только по holdout.
    top_models = sorted(scores, key=lambda m: scores[m][1], reverse=True)[:2]
    try:
        baseline_holdout = await holdout_score(holdout, current_instructions)
        final_scores: dict[str, float] = {}
        for model_name in top_models:
            final_scores[model_name] = await holdout_score(holdout, scores[model_name][0])
    except Exception as exc:
        logger.warning("Optimizer: holdout evaluation failed: %s", exc)
        if progress_msg:
            await _update_progress(progress_msg, 78, f"❌ Ошибка holdout-оценки: {exc}")
        return

    winner_model = max(final_scores, key=lambda m: final_scores[m])
    winner_content = scores[winner_model][0]
    winner_score = final_scores[winner_model]
    baseline = baseline_holdout  # гейт и отчёт сравнивают holdout с holdout

    if progress_msg:
        await _update_progress(progress_msg, 80, "Сохраняю кандидатов...")

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
        if progress_msg:
            try:
                await progress_msg.edit_text(
                    f"⚙️ <b>Оптимизация промпта</b>\n\n"
                    f"{_progress_bar(100)}\n\n"
                    f"✅ Завершено. Улучшений не найдено.\n"
                    f"Базовый скор: {round(baseline * 100)} баллов · "
                    f"Лучший кандидат: {round(winner_score * 100)} баллов ({winner_model}).",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    if progress_msg:
        await _update_progress(progress_msg, 95, "Формирую отчёт...")

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

    if progress_msg:
        try:
            await progress_msg.edit_text(
                f"⚙️ <b>Оптимизация промпта</b>\n\n"
                f"{_progress_bar(100)}\n\n"
                f"✅ Готово! Отчёт отправлен. Победитель: <b>{escape(winner_model)}</b> "
                f"({round(winner_score * 100)} баллов, +{round((winner_score - baseline) / max(baseline, 0.01) * 100)}%).",
                parse_mode="HTML",
            )
        except Exception:
            pass


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

    old_lines = current_instructions.splitlines()
    new_lines = winner_content.splitlines()
    # Find first line that differs for a meaningful snippet
    old_snippet = new_snippet = ""
    for ol, nl in zip(old_lines, new_lines):
        if ol.strip() != nl.strip():
            old_snippet = ol.strip()[:120]
            new_snippet = nl.strip()[:120]
            break
    if not old_snippet:
        # No line-level diff found — fall back to start of each
        old_snippet = " ".join(old_lines[:3])[:120]
        new_snippet = " ".join(new_lines[:3])[:120]

    text = (
        "\U0001f9ea <b>Ночная оптимизация промпта</b>\n\n"
        f"\U0001f4ca Данные: {sample_count} тикетов · 30 дней\n"
        f"\u26a1 Сейчас: {baseline_int} баллов\n\n"
        f"\U0001f947 Победитель: <b>{escape(winner_model)}</b>\n"
        f"\U0001f4c8 Результат: {winner_int} баллов (+{improvement_pct}%)\n\n"
        f"\U0001f4dd <b>Первое отличие:</b>\n"
        f"— <i>{escape(old_snippet)}</i>\n"
        f"+ <i>{escape(new_snippet)}</i>\n\n"
        f"Нажми <b>📊 Подробнее</b> чтобы увидеть оба промпта целиком."
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

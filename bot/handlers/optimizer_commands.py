"""Prompt-optimizer commands: /aioptimize, /promptrollback and opt:/rollback: callbacks."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db
from ..ai_summary import invalidate_prompt_cache

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("aioptimize"))
async def cmd_aioptimize(message: Message) -> None:
    """Manually trigger prompt optimizer (for testing)."""
    from ..optimizer.agent import run_optimizer
    from ..config import config as _cfg
    if message.from_user and message.from_user.id not in _cfg.operator_telegram_user_ids:
        return
    await message.answer("🧪 Запускаю оптимизатор промптов...")
    import asyncio
    asyncio.create_task(run_optimizer(message.bot))


@router.callback_query(F.data.startswith("opt:apply:"))
async def cb_opt_apply(callback: CallbackQuery) -> None:
    try:
        version_id = int(callback.data.split(":")[-1])
        await db.apply_prompt_version(version_id)
        invalidate_prompt_cache()
        await callback.answer("✅ Новый промпт применён", show_alert=True)
        try:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n<i>✅ Применено</i>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(f"❌ Ошибка: {exc}", show_alert=True)


@router.callback_query(F.data == "opt:reject")
async def cb_opt_reject(callback: CallbackQuery) -> None:
    from ..db import reject_all_prompt_candidates
    await reject_all_prompt_candidates()
    await callback.answer("❌ Отклонено", show_alert=False)
    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n<i>❌ Отклонено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("opt:detail:"))
async def cb_opt_detail(callback: CallbackQuery) -> None:
    from html import escape
    from ..db import get_prompt_version, get_optimization_samples
    from ..ai_summary import get_active_format_instructions
    await callback.answer()
    try:
        version_id = int(callback.data.split(":")[-1])
        version = await get_prompt_version(version_id)
        if not version:
            await callback.message.answer("❌ Версия промпта не найдена.")
            return

        current = await get_active_format_instructions()
        new_text = version["content"]

        # Sample breakdown
        samples = await get_optimization_samples(days=30)
        by_outcome: dict[str, int] = {}
        for s in samples:
            by_outcome[s["outcome"]] = by_outcome.get(s["outcome"], 0) + 1

        outcome_line = "  ".join(
            f"{o}: {c}" for o, c in sorted(by_outcome.items())
        )

        # Send full old prompt
        old_msg = f"📄 <b>Текущий промпт:</b>\n\n<pre>{escape(current[:3800])}</pre>"
        await callback.message.answer(old_msg, parse_mode="HTML")

        # Send full new prompt
        score_str = f"{round(version['score'] * 100)} баллов" if version.get("score") else "—"
        new_msg = (
            f"✨ <b>Предложенный промпт</b> ({escape(version['proposed_by'])}, {score_str}):\n\n"
            f"<pre>{escape(new_text[:3800])}</pre>\n\n"
            f"📊 Сэмплы: {len(samples)} ({outcome_line})"
        )
        await callback.message.answer(new_msg, parse_mode="HTML")

    except Exception as exc:
        await callback.message.answer(f"❌ Ошибка: {exc}")


@router.message(Command("promptrollback"))
async def cmd_promptrollback(message: Message) -> None:
    from html import escape
    from ..db import list_prompt_versions
    from ..config import config as _cfg
    if message.from_user and message.from_user.id not in _cfg.operator_telegram_user_ids:
        return

    versions = await list_prompt_versions(limit=8)
    if not versions:
        await message.answer("❌ Нет сохранённых версий промпта.")
        return

    lines = ["📋 <b>Версии промпта</b> (новейшие сверху):\n"]
    buttons = []
    for v in versions:
        status_icon = "✅" if v["status"] == "active" else ("🔄" if v["status"] == "candidate" else "⬛")
        score_str = f"{round(v['score'] * 100)}%" if v.get("score") else "—"
        date_str = (v.get("created_at") or "")[:16]
        preview = escape((v["content"] or "")[:80].replace("\n", " "))
        lines.append(
            f"{status_icon} <b>#{v['id']}</b> · {score_str} · {date_str}\n"
            f"<i>{preview}…</i>"
        )
        if v["status"] != "active":
            buttons.append([InlineKeyboardButton(
                text=f"⬅️ Применить #{v['id']}",
                callback_data=f"rollback:apply:{v['id']}",
            )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("rollback:apply:"))
async def cb_rollback_apply(callback: CallbackQuery) -> None:
    from ..db import apply_prompt_version, get_prompt_version
    from ..ai_summary import invalidate_prompt_cache
    await callback.answer()
    try:
        version_id = int(callback.data.split(":")[-1])
        version = await get_prompt_version(version_id)
        if not version:
            await callback.answer("❌ Версия не найдена", show_alert=True)
            return
        await apply_prompt_version(version_id)
        invalidate_prompt_cache()
        score_str = f"{round(version['score'] * 100)}%" if version.get("score") else "—"
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ <b>Применена версия #{version_id}</b> (score: {score_str})",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as exc:
        await callback.answer(f"❌ Ошибка: {exc}", show_alert=True)

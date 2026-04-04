from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..db import (
    count_active_topics,
    count_pending_delete_topics,
    count_pending_pre_sla_topics,
    count_total_topics,
)
from ..formatter import format_refresh_result
from ..refresh import refresh_topics
from ..operator_replies import (
    OperatorReplyError,
    add_internal_note,
    cache_incoming_topic_media,
    delete_operator_message,
    edit_operator_message,
    format_operator_exception,
    get_operator_topic_context,
    send_public_reply,
)

router = Router()


async def _run_operator_command(
    message: Message,
    action,
) -> None:
    try:
        result = await action()
    except Exception as exc:
        await message.answer(format_operator_exception(exc), parse_mode="HTML")
        return

    await message.answer(result, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>HDE Notification Router</b>\n\n"
        "Бот отслеживает ваши тикеты из HelpDeskEddy, ведет Telegram topics,\n"
        "считает pre-SLA напоминания и поддерживает /note и /send для текста, медиа и альбомов.",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    active_topics = await count_active_topics()
    pending_delete = await count_pending_delete_topics()
    pending_pre_sla = await count_pending_pre_sla_topics()
    total_topics = await count_total_topics()

    await message.answer(
        "📊 <b>Статус бота</b>\n\n"
        f"🟢 Активных topics: <b>{active_topics}</b>\n"
        f"⏳ Ожидают удаления: <b>{pending_delete}</b>\n"
        f"⏰ Ожидают pre-SLA: <b>{pending_pre_sla}</b>\n"
        f"📚 Всего записей: <b>{total_topics}</b>",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Команды бота</b>\n\n"
        "/start — краткое описание бота\n"
        "/status — активные topics, pending delete и pre-SLA\n"
        "/help — этот список команд\n"
        "/note текст — добавить внутренний комментарий в HDE\n"
        "/note в ответ на фото, видео, voice, документ или альбом — сохранить медиа в комментарий\n"
        "/send текст — отправить публичный ответ клиенту через HDE\n"
        "/send в ответ на сообщение, медиа или альбом — отправить текст, caption и вложения клиенту\n"
        "/delete в ответ на сообщение — удалить его из HDE\n"
        "/refresh — синхронизировать топики с HDE, убрать устаревшие",
        parse_mode="HTML",
    )


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        return await add_internal_note(
            bot=message.bot,
            context=context,
            message=message,
            command_args=command.args,
            tg_message_id=message.message_id,
        )

    await _run_operator_command(message, action)


@router.message(Command("send"))
async def cmd_send(message: Message, command: CommandObject) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        return await send_public_reply(
            bot=message.bot,
            context=context,
            message=message,
            command_args=command.args,
            tg_message_id=message.message_id,
        )

    await _run_operator_command(message, action)


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        reply = message.reply_to_message
        if reply is None:
            raise OperatorReplyError("Ответьте командой /delete на сообщение, которое хотите удалить из HDE")
        return await delete_operator_message(
            context=context,
            telegram_message_id=reply.message_id,
        )

    await _run_operator_command(message, action)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    wait_msg = await message.answer("🔄 Синхронизирую с HDE...")
    try:
        result = await refresh_topics(bot=message.bot)
    except Exception as exc:
        await wait_msg.delete()
        await message.answer(f"⚠️ <b>Ошибка при синхронизации:</b> {exc}", parse_mode="HTML")
        return
    await wait_msg.delete()
    text = format_refresh_result(
        active_count=result.active_after,
        marked_deleted=result.marked_deleted,
        pending_delete_count=result.pending_delete_count,
    )
    await message.answer(text, parse_mode="HTML")


@router.edited_message(F.message_thread_id.is_not(None))
async def on_edited_message(message: Message) -> None:
    """When operator edits a message in a topic, sync the edit to HDE."""
    if message.from_user is None or message.from_user.is_bot:
        return

    async def action() -> str:
        context = await get_operator_topic_context(
            telegram_user_id=message.from_user.id,
            topic_id=message.message_thread_id,
        )
        new_text = (message.text or message.caption or "").strip()
        return await edit_operator_message(
            context=context,
            telegram_message_id=message.message_id,
            new_text=new_text,
        )

    try:
        result = await action()
    except OperatorReplyError:
        # Silently ignore — most edits in topics are unrelated to HDE
        return
    except Exception:
        return

    await message.answer(result, parse_mode="HTML")


@router.message()
async def cache_topic_media(message: Message) -> None:
    await cache_incoming_topic_media(message)

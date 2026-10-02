from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import Message

from . import db
from .config import config
from .formatter import (
    format_note_saved,
    format_operator_error,
    format_reply_sent,
    format_message_edited,
    format_message_deleted,
)
from .hde_api import HDEApiClient, HDEApiError, HDEAttachment
from .operators import hde_auth_for_user


def _extract_hde_id(result: Any) -> Optional[int]:
    """Extract numeric ID from HDE API response: {"data": {"id": 42, ...}}"""
    if hasattr(result, "data"):
        data = result.data
        if isinstance(data, dict):
            inner = data.get("data", {})
            if isinstance(inner, dict):
                return inner.get("id")
    return None


class OperatorReplyError(RuntimeError):
    pass


@dataclass
class OperatorTopicContext:
    record: db.TicketTopic
    telegram_user_id: int
    topic_id: int
    chat_id: int


@dataclass(frozen=True)
class PreparedOutboundMessage:
    text: str
    attachments: tuple[HDEAttachment, ...]


def extract_command_text(command_args: Optional[str], replied_text: Optional[str] = None) -> str:
    text = (command_args or "").strip()
    if text:
        return text
    return (replied_text or "").strip()


async def get_operator_topic_context(
    *,
    telegram_user_id: int,
    topic_id: Optional[int],
    chat_id: int,
) -> OperatorTopicContext:
    if topic_id is None:
        raise OperatorReplyError("Команда доступна только внутри topic")

    if not config.is_operator_allowed(telegram_user_id):
        raise OperatorReplyError("У вас нет прав для этой команды")

    record = await db.get_topic_by_topic_id(chat_id, topic_id)
    if record is None or record.is_deleted:
        raise OperatorReplyError("Topic не связан с активным тикетом")

    if not record.is_active:
        raise OperatorReplyError("Topic не активен для ответа")

    return OperatorTopicContext(
        record=record,
        telegram_user_id=telegram_user_id,
        topic_id=topic_id,
        chat_id=chat_id,
    )


def _message_text(message: Optional[Message]) -> str:
    if message is None:
        return ""
    return (message.text or message.caption or "").strip()


def _pick_attachment_meta(message: Message) -> tuple[str, str, str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, "photo", "photo.jpg", "image/jpeg"
    if message.document:
        return (
            message.document.file_id,
            "document",
            message.document.file_name or "document.bin",
            message.document.mime_type or "application/octet-stream",
        )
    if message.video:
        return (
            message.video.file_id,
            "video",
            message.video.file_name or "video.mp4",
            message.video.mime_type or "video/mp4",
        )
    if message.voice:
        return message.voice.file_id, "voice", "voice.ogg", message.voice.mime_type or "audio/ogg"
    if message.audio:
        return (
            message.audio.file_id,
            "audio",
            message.audio.file_name or "audio.mp3",
            message.audio.mime_type or "audio/mpeg",
        )
    if message.animation:
        return (
            message.animation.file_id,
            "animation",
            message.animation.file_name or "animation.mp4",
            message.animation.mime_type or "video/mp4",
        )
    if message.video_note:
        return message.video_note.file_id, "video_note", "video_note.mp4", "video/mp4"
    return None


async def cache_incoming_topic_media(message: Message) -> None:
    if message.message_thread_id is None:
        return
    if message.from_user and message.from_user.is_bot:
        return
    if (message.text or "").startswith("/"):
        return

    attachment = _pick_attachment_meta(message)
    if attachment is None:
        return

    file_id, attachment_kind, filename, content_type = attachment
    await db.cache_topic_media(
        chat_id=message.chat.id,
        topic_id=message.message_thread_id,
        message_id=message.message_id,
        media_group_id=message.media_group_id,
        attachment_kind=attachment_kind,
        file_id=file_id,
        filename=filename,
        content_type=content_type,
        text=_message_text(message),
    )


async def _download_attachment(
    bot: Bot,
    *,
    file_id: str,
    filename: str,
    content_type: str,
) -> HDEAttachment:
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return HDEAttachment(
        filename=filename,
        content=buffer.getvalue(),
        content_type=content_type or "application/octet-stream",
    )


async def _extract_cached_media_group(
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
) -> tuple[str, tuple[HDEAttachment, ...]]:
    media_group_id = getattr(message, "media_group_id", None)
    if not media_group_id:
        return "", ()

    cached_items = await db.list_cached_topic_media_group(context.chat_id, context.topic_id, media_group_id)
    if not cached_items:
        return "", ()

    attachments: list[HDEAttachment] = []
    fallback_text = ""
    for item in cached_items:
        attachments.append(
            await _download_attachment(
                bot,
                file_id=item.file_id,
                filename=item.filename or "attachment.bin",
                content_type=item.content_type or "application/octet-stream",
            )
        )
        if not fallback_text and item.text:
            fallback_text = item.text
    return fallback_text, tuple(attachments)


async def _extract_single_attachment(bot: Bot, message: Optional[Message]) -> tuple[HDEAttachment, ...]:
    if message is None:
        return ()

    attachment = _pick_attachment_meta(message)
    if attachment is None:
        return ()

    file_id, _kind, filename, content_type = attachment
    return (
        await _download_attachment(
            bot,
            file_id=file_id,
            filename=filename,
            content_type=content_type,
        ),
    )


async def prepare_outbound_message(
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
    command_args: Optional[str],
) -> PreparedOutboundMessage:
    source_message = message.reply_to_message or message
    group_text, group_attachments = await _extract_cached_media_group(bot, context, source_message)
    attachments = group_attachments or await _extract_single_attachment(bot, source_message)
    fallback_text = group_text or _message_text(message.reply_to_message)
    text = extract_command_text(command_args, fallback_text)
    return PreparedOutboundMessage(text=text, attachments=attachments)


def _ensure_payload_present(payload: PreparedOutboundMessage, error_message: str) -> None:
    if payload.text or payload.attachments:
        return
    raise OperatorReplyError(error_message)


async def add_internal_note(
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
    command_args: Optional[str],
    tg_message_id: Optional[int] = None,
) -> str:
    payload = await prepare_outbound_message(bot, context, message, command_args)
    _ensure_payload_present(
        payload,
        "Укажите текст заметки после команды или ответьте командой на сообщение с текстом или медиа",
    )

    client = HDEApiClient(auth=hde_auth_for_user(context.telegram_user_id))
    try:
        result = await client.add_comment(
            context.record.ticket_id,
            text=payload.text,
            attachments=payload.attachments,
        )
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    hde_comment_id = _extract_hde_id(result)
    if hde_comment_id is not None and tg_message_id is not None:
        await db.save_sent_message(
            chat_id=context.chat_id,
            telegram_message_id=tg_message_id,
            topic_id=context.topic_id,
            ticket_id=context.record.ticket_id,
            hde_entity_id=hde_comment_id,
            entity_type="comment",
        )

    return format_note_saved(context.record.unique_id)


async def send_public_reply(
    *,
    bot: Bot,
    context: OperatorTopicContext,
    message: Message,
    command_args: Optional[str],
    tg_message_id: Optional[int] = None,
) -> str:
    if not config.public_reply_enabled:
        raise OperatorReplyError("Публичные ответы из topic отключены в конфигурации")

    if not config.is_public_reply_allowed(context.record.ticket_id, context.record.unique_id):
        raise OperatorReplyError("Этот тикет не разрешен для публичных ответов из Telegram")

    payload = await prepare_outbound_message(bot, context, message, command_args)
    _ensure_payload_present(
        payload,
        "Укажите текст после /send или ответьте командой на сообщение с текстом или медиа",
    )

    client = HDEApiClient(auth=hde_auth_for_user(context.telegram_user_id))
    try:
        result = await client.add_post(
            context.record.ticket_id,
            text=payload.text,
            attachments=payload.attachments,
        )
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    hde_post_id = _extract_hde_id(result)
    if hde_post_id is not None and tg_message_id is not None:
        await db.save_sent_message(
            chat_id=context.chat_id,
            telegram_message_id=tg_message_id,
            topic_id=context.topic_id,
            ticket_id=context.record.ticket_id,
            hde_entity_id=hde_post_id,
            entity_type="post",
        )

    return format_reply_sent(context.record.unique_id)


def format_operator_exception(exc: Exception) -> str:
    if isinstance(exc, OperatorReplyError):
        return format_operator_error(str(exc))
    if isinstance(exc, HDEApiError):
        return format_operator_error(str(exc))
    return format_operator_error("Не удалось выполнить команду")


async def edit_operator_message(
    *,
    context: OperatorTopicContext,
    telegram_message_id: int,
    new_text: str,
) -> str:
    """Called when operator edits a Telegram message that was sent to HDE."""
    record = await db.get_sent_message(context.chat_id, telegram_message_id, context.topic_id)
    if record is None:
        raise OperatorReplyError("Это сообщение не связано с HDE")

    if not new_text.strip():
        raise OperatorReplyError("Текст не может быть пустым")

    client = HDEApiClient(auth=hde_auth_for_user(context.telegram_user_id))
    try:
        if record.entity_type == "post":
            await client.update_post(record.ticket_id, record.hde_entity_id, new_text)
        else:
            await client.update_comment(record.ticket_id, record.hde_entity_id, new_text)
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    ticket = await db.get_topic(record.ticket_id)
    display_id = ticket.unique_id if ticket else record.ticket_id
    return format_message_edited(display_id)


async def delete_operator_message(
    *,
    context: OperatorTopicContext,
    telegram_message_id: int,
) -> str:
    """Called when operator uses /delete replying to a message sent to HDE."""
    record = await db.get_sent_message(context.chat_id, telegram_message_id, context.topic_id)
    if record is None:
        raise OperatorReplyError("Это сообщение не связано с HDE")

    client = HDEApiClient(auth=hde_auth_for_user(context.telegram_user_id))
    try:
        if record.entity_type == "post":
            await client.delete_post(record.ticket_id, record.hde_entity_id)
        else:
            await client.delete_comment(record.ticket_id, record.hde_entity_id)
    except HDEApiError as exc:
        raise OperatorReplyError(str(exc)) from exc

    await db.delete_sent_message(context.chat_id, telegram_message_id, context.topic_id)

    ticket = await db.get_topic(record.ticket_id)
    display_id = ticket.unique_id if ticket else record.ticket_id
    return format_message_deleted(display_id)

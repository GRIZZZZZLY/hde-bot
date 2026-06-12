"""Client attachment delivery and photo (Vision) description posting.

Mechanically extracted from bot.topic_manager (pure move, no behavior change).
Shared state and test patch-points (config, db, download_client_attachment,
helpers) are accessed late-bound through the bot.topic_manager module object
so monkeypatches on bot.topic_manager keep working.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo

from .client_media import detect_telegram_media_kind

logger = logging.getLogger(__name__)


async def _send_client_attachments(bot: Bot, topic_id: int, payload: dict) -> None:
    from . import topic_manager as _tm
    attachment_refs = list(payload.get("attachments") or [])
    if not attachment_refs:
        return

    photo_video_media: list[tuple[str, BufferedInputFile]] = []
    single_items: list[tuple[str, BufferedInputFile]] = []
    photo_blobs: list[tuple[bytes, str]] = []  # (content, filename) for Vision

    for ref in attachment_refs:
        try:
            attachment = await _tm.download_client_attachment(ref)
        except Exception as exc:
            logger.error("Failed to download client attachment for ticket %s: %s", _tm._payload_value(payload, "ticket_id"), exc)
            continue

        kind = detect_telegram_media_kind(attachment)
        input_file = BufferedInputFile(attachment.content, filename=attachment.filename)
        if kind in {"photo", "video"}:
            photo_video_media.append((kind, input_file))
            if kind == "photo":
                photo_blobs.append((attachment.content, attachment.filename))
        else:
            single_items.append((kind, input_file))

    for chunk in _tm._chunked(photo_video_media, 10):
        if len(chunk) == 1:
            kind, media = chunk[0]
            if kind == "photo":
                await bot.send_photo(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    photo=media,
                )
            else:
                await bot.send_video(
                    chat_id=_tm.config.group_chat_id,
                    message_thread_id=topic_id,
                    video=media,
                )
            continue

        media_group = []
        for kind, media in chunk:
            if kind == "photo":
                media_group.append(InputMediaPhoto(media=media))
            else:
                media_group.append(InputMediaVideo(media=media))
        await bot.send_media_group(
            chat_id=_tm.config.group_chat_id,
            message_thread_id=topic_id,
            media=media_group,
        )

    for kind, media in single_items:
        if kind == "voice":
            await bot.send_voice(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                voice=media,
            )
        elif kind == "audio":
            await bot.send_audio(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                audio=media,
            )
        else:
            await bot.send_document(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                document=media,
            )

    if photo_blobs:
        await _describe_and_post_photos(bot, topic_id, photo_blobs, payload)


async def _describe_and_post_photos(
    bot: Bot,
    topic_id: int,
    photos: list[tuple[bytes, str]],
    payload: dict,
) -> None:
    """Describe photos via Vision and post a single 🔍 summary message to the topic."""
    from . import topic_manager as _tm
    from .vision import describe_image  # local import: keep vision lazy

    tasks = [describe_image(content, filename) for content, filename in photos]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.warning("vision: gather failed for ticket %s: %s", _tm._payload_value(payload, "ticket_id"), exc)
        return

    descriptions: list[str] = []
    for r in results:
        if isinstance(r, str) and r.strip():
            descriptions.append(r.strip())

    if not descriptions:
        return

    ticket_id = str(_tm._payload_value(payload, "ticket_id") or "")
    if ticket_id:
        try:
            await _tm.db.append_photo_descriptions(ticket_id, descriptions)
        except Exception as exc:
            logger.warning("vision: failed to persist descriptions for ticket %s: %s", ticket_id, exc)

    if len(descriptions) == 1:
        text = f"🔍 На фото: {descriptions[0]}"
    else:
        lines = "\n".join(f"{i}. {d}" for i, d in enumerate(descriptions, 1))
        text = f"🔍 На фото:\n{lines}"

    try:
        await _tm._send_topic_message(bot, topic_id, text)
    except Exception as exc:
        logger.warning("vision: failed to post description for ticket %s: %s", ticket_id, exc)

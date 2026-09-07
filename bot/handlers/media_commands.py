"""Topic media handlers: call-recording transcription and the media cache catch-all.

The catch-all @router.message() must stay in the LAST router included into the
commands router — otherwise it would swallow command messages.
"""
import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.types import Message

from .. import db
from ..operator_replies import cache_incoming_topic_media
from ..transcription import extract_audio_meta, process_call_note, process_call_recording

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.message_thread_id.is_not(None), F.voice)
async def handle_topic_voice_note(message: Message) -> None:
    """Оператор наговаривает заметку после звонка: она становится контекстом тикета.

    Записи звонков (audio/document) уходят в базу знаний, а это — про конкретного
    клиента, своими словами. В общий retrieval такое пускать нельзя: там оно
    всплывёт в чужом обращении как факт. Поэтому только call_notes своего тикета,
    откуда его читают черновик и ночная сверка.
    """
    await cache_incoming_topic_media(message)

    if not message.from_user or message.from_user.is_bot:
        return
    from ..config import config
    if not config.is_operator_allowed(message.from_user.id):
        return
    if not config.agent_call_fixation_enabled:
        return
    record = await db.get_topic_by_topic_id(message.message_thread_id)
    if record is None or record.is_deleted:
        return

    buffer = BytesIO()
    try:
        await message.bot.download(message.voice.file_id, destination=buffer)
    except Exception as exc:
        logger.warning("Voice note download failed: %s", exc)
        return

    status = await message.reply("🎙️ Записываю заметку по тикету...")
    try:
        note = await process_call_note(
            buffer.getvalue(),
            message.voice.mime_type or "audio/ogg",
            ticket_id=record.ticket_id,
            ticket_title=record.ticket_name,
        )
    except Exception as exc:
        logger.warning("Voice note processing failed for ticket %s: %s",
                       record.ticket_id, exc)
        note = None
    if note:
        await status.edit_text(f"📝 Записал в контекст тикета:\n{note[:600]}")
    else:
        await status.edit_text("⚠️ Не удалось разобрать заметку")


@router.message(F.message_thread_id.is_not(None), F.audio | F.document)
async def handle_topic_call_recording(message: Message) -> None:
    """Operator drops a call recording (.wav/.mp3) into a ticket topic:
    transcribe via Deepgram, distill via Groq, index as source='transcription'.
    """
    # Keep attach-to-reply behavior: this handler shadows the catch-all below.
    await cache_incoming_topic_media(message)

    meta = extract_audio_meta(message)
    if meta is None:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    from ..config import config
    if not config.is_operator_allowed(message.from_user.id):
        return
    record = await db.get_topic_by_topic_id(message.message_thread_id)
    if record is None or record.is_deleted:
        return

    file_id, mime_type = meta
    buffer = BytesIO()
    try:
        await message.bot.download(file_id, destination=buffer)
    except Exception as exc:
        logger.warning("Call recording download failed: %s", exc)
        return

    status = await message.reply("🎙️ Транскрибирую звонок...")
    try:
        item_id = await process_call_recording(
            buffer.getvalue(),
            mime_type,
            ticket_id=record.ticket_id,
            ticket_title=record.ticket_name,
        )
    except Exception as exc:
        logger.warning("Call recording processing failed for ticket %s: %s",
                       record.ticket_id, exc)
        item_id = None
    if item_id:
        await status.edit_text("🎙️ Звонок транскрибирован — добавлен в базу знаний")
    else:
        await status.edit_text("⚠️ Не удалось транскрибировать звонок")


@router.message()
async def cache_topic_media(message: Message) -> None:
    await cache_incoming_topic_media(message)

"""Call recording pipeline: Deepgram transcription -> Groq summary -> knowledge index.

Per spec docs/superpowers/specs/2026-04-11-ai-knowledge-rag-design.md (раздел
"Транскрипция звонков"): оператор отправляет запись звонка (.wav/.mp3) в топик
тикета, бот транскрибирует через Deepgram, выжимает суть через Groq и
индексирует в knowledge_items с source='transcription'.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp

from .config import config
from .hde_api import shared_session
from .knowledge.indexer import index_knowledge_item
from .llm_semaphore import LLM_SEMAPHORE

if TYPE_CHECKING:
    from aiogram.types import Message

logger = logging.getLogger(__name__)

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Модель — из env вместе с суммаркой (config.groq_summary_model):
# жёстко прописанный llama-3.3-70b Groq вывел из обслуживания (404).

# Расширение -> MIME для документов без внятного mime_type
_AUDIO_EXT_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
}

_SUMMARY_SYSTEM = (
    "Ты помощник техподдержки POS-оборудования. Тебе дают сырой транскрипт "
    "телефонного звонка клиента с оператором. Сделай краткую выжимку для базы "
    "знаний строго в формате:\n"
    "Проблема: <с чем обратился клиент, 1-2 предложения>\n"
    "Решение: <что сделал/посоветовал оператор, по шагам если их несколько>\n"
    "Пиши только факты из звонка, без воды. Если решение в звонке не "
    "прозвучало, напиши «Решение: не зафиксировано в звонке»."
)


def extract_audio_meta(message: "Message") -> tuple[str, str] | None:
    """Return (file_id, mime_type) if the message carries a call recording.

    Audio messages and documents with audio extension qualify; voice notes are
    skipped — they are operator reply material, not call recordings.
    """
    audio = getattr(message, "audio", None)
    if audio is not None:
        name = (getattr(audio, "file_name", None) or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        mime = audio.mime_type or _AUDIO_EXT_MIME.get(ext, "audio/mpeg")
        return audio.file_id, mime
    doc = getattr(message, "document", None)
    if doc is not None:
        mime = (doc.mime_type or "").lower()
        name = (getattr(doc, "file_name", None) or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if mime.startswith("audio/"):
            return doc.file_id, mime
        if ext in _AUDIO_EXT_MIME:
            return doc.file_id, _AUDIO_EXT_MIME[ext]
    return None


async def transcribe_audio(
    audio_data: bytes, mime_type: str, session: aiohttp.ClientSession
) -> str | None:
    """Transcribe audio bytes via Deepgram (ru, nova-2). None on any failure."""
    if not config.deepgram_api_key:
        logger.info("Transcription skipped: DEEPGRAM_API_KEY not set")
        return None
    try:
        async with LLM_SEMAPHORE:
            async with session.post(
                _DEEPGRAM_URL,
                data=audio_data,
                headers={
                    "Authorization": f"Token {config.deepgram_api_key}",
                    "Content-Type": mime_type,
                },
                params={"language": "ru", "model": "nova-2", "smart_format": "true"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Deepgram error %s: %s", resp.status, body[:200])
                    return None
                result = await resp.json()
    except Exception as exc:
        logger.warning("Deepgram transcription failed: %s", exc)
        return None
    transcript = (
        result.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
        .strip()
    )
    return transcript or None


async def summarize_transcript(
    transcript: str, ticket_title: str, session: aiohttp.ClientSession
) -> str | None:
    """Distill raw call transcript into 'Проблема/Решение' via Groq."""
    if not config.groq_api_key:
        logger.info("Transcript summary skipped: GROQ_API_KEY not set")
        return None
    try:
        async with LLM_SEMAPHORE:
            async with session.post(
                _GROQ_URL,
                json={
                    "model": config.groq_summary_model,
                    "messages": [
                        {"role": "system", "content": _SUMMARY_SYSTEM},
                        {
                            "role": "user",
                            "content": (
                                f"Тикет: {ticket_title}\n\n"
                                f"Транскрипт звонка:\n{transcript[:12000]}"
                            ),
                        },
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.2,
                    **({"reasoning_effort": config.groq_reasoning_effort}
                       if config.groq_reasoning_effort else {}),
                },
                headers={"Authorization": f"Bearer {config.groq_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq summary error %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        return data["choices"][0]["message"]["content"].strip() or None
    except Exception as exc:
        logger.warning("Transcript summary failed: %s", exc)
        return None


_NOTE_SYSTEM = (
    "Ты помощник инженера техподдержки POS-оборудования. Оператор наговорил "
    "заметку после телефонного разговора с клиентом. Выжми из неё факты, "
    "которые понадобятся при ответе клиенту: модель оборудования и ПО, банк или "
    "ОФД, код ошибки, что уже проверили, что клиенту обещали. Пиши одним "
    "абзацем, только факты из заметки, без вступлений и без выдумок. Если "
    "фактов нет — верни пустую строку."
)


async def _transcribe(audio_data: bytes, mime_type: str) -> str | None:
    async with shared_session() as session:
        return await transcribe_audio(audio_data, mime_type, session)


async def _distill(transcript: str, ticket_title: str = "") -> str | None:
    async with shared_session() as session:
        return await summarize_transcript(transcript, ticket_title, session)


async def _distill_note(transcript: str, ticket_title: str = "") -> str | None:
    """Выжимка голосовой заметки оператора — другой промпт, чем у записи звонка.

    У записи звонка формат «Проблема/Решение» для базы знаний. Заметка нужна для
    контекста черновика: важны факты, которые оператор уже узнал, чтобы бот их
    не переспрашивал.
    """
    from .ai_summary import call_groq_text

    return await call_groq_text(
        f"Тикет: {ticket_title}\n\nЗаметка оператора:\n{transcript[:6000]}",
        system=_NOTE_SYSTEM, model=config.groq_summary_model,
        max_tokens=400, temperature=0.2,
        reasoning_effort=config.groq_reasoning_effort or None,
    )


async def process_call_recording(
    audio_data: bytes,
    mime_type: str,
    *,
    ticket_id: str,
    ticket_title: str,
    _transcribe_fn=None,
    _distill_fn=None,
    _index_fn=None,
) -> int | None:
    """Transcribe -> summarize -> index. Returns knowledge item id or None.

    Выжимка дополнительно ложится в call_notes своего тикета: канал базы знаний
    остаётся как был, но теперь запись звонка виден и черновику по этому тикету,
    и ночной сверке — раньше она знала о звонке только если тот случайно всплыл
    через retrieval.
    """
    transcribe = _transcribe_fn or _transcribe
    distill = _distill_fn or _distill
    index = _index_fn or index_knowledge_item

    transcript = await transcribe(audio_data, mime_type)
    if not transcript:
        return None
    logger.info(
        "Call transcribed for ticket %s (%d chars): %r...",
        ticket_id, len(transcript), transcript[:80],
    )
    summary = await distill(transcript, ticket_title)
    if not summary:
        return None
    await _store_call_notes(ticket_id, summary)
    return await index(
        "transcription",
        summary,
        ticket_id=ticket_id,
        title=ticket_title,
    )


async def process_call_note(
    audio_data: bytes,
    mime_type: str,
    *,
    ticket_id: str,
    ticket_title: str = "",
    _transcribe_fn=None,
    _distill_fn=None,
    _index_fn=None,
) -> str | None:
    """Голосовая заметка оператора после звонка → контекст ЭТОГО тикета.

    В общую базу знаний не идёт намеренно: заметка про конкретного клиента,
    сказанная своими словами, во чужом обращении читается как факт о нём.
    Возвращает записанную выжимку или None (флаг выключен, транскрипция или
    выжимка не удались).
    """
    if not config.agent_call_fixation_enabled:
        return None
    transcribe = _transcribe_fn or _transcribe
    distill = _distill_fn or _distill_note

    transcript = await transcribe(audio_data, mime_type)
    if not transcript:
        return None
    note = await distill(transcript, ticket_title)
    note = (note or "").strip()
    if not note:
        return None
    await _store_call_notes(ticket_id, note)
    logger.info("Call note stored for ticket %s (%d chars)", ticket_id, len(note))
    return note


async def _store_call_notes(ticket_id: str, note: str) -> None:
    """Промах записи не должен ронять транскрипцию: текст уже получен и полезен."""
    from . import db

    try:
        await db.append_call_notes(str(ticket_id), [note])
    except Exception as exc:
        logger.warning("call_notes append failed for ticket %s: %s", ticket_id, exc)

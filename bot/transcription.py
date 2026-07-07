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
_GROQ_MODEL = "llama-3.3-70b-versatile"

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
    """Distill raw call transcript into 'Проблема/Решение' via Groq llama-3.3."""
    if not config.groq_api_key:
        logger.info("Transcript summary skipped: GROQ_API_KEY not set")
        return None
    try:
        async with LLM_SEMAPHORE:
            async with session.post(
                _GROQ_URL,
                json={
                    "model": _GROQ_MODEL,
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


async def process_call_recording(
    audio_data: bytes,
    mime_type: str,
    *,
    ticket_id: str,
    ticket_title: str,
) -> int | None:
    """Transcribe -> summarize -> index. Returns knowledge item id or None."""
    async with shared_session() as session:
        transcript = await transcribe_audio(audio_data, mime_type, session)
        if not transcript:
            return None
        logger.info(
            "Call transcribed for ticket %s (%d chars): %r...",
            ticket_id, len(transcript), transcript[:80],
        )
        summary = await summarize_transcript(transcript, ticket_title, session)
        if not summary:
            return None
    return await index_knowledge_item(
        "transcription",
        summary,
        ticket_id=ticket_id,
        title=ticket_title,
    )

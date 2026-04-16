"""Vision-RAG: describe images via Gemini 2.5 Flash for knowledge indexing."""
from __future__ import annotations

import base64
import logging

import aiohttp

from .config import config
from .llm_semaphore import LLM_SEMAPHORE

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)

_PROMPT = (
    "Это скриншот из обращения в техподдержку кассового оборудования "
    "(АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).\n"
    "Прочитай текст ошибки/сообщения на картинке и кратко опиши проблему "
    "в 1–2 предложениях на русском. Если текста нет — опиши, что видно. "
    "Не добавляй вступлений и пояснений, только суть."
)

_MAX_BYTES = 4 * 1024 * 1024  # Gemini inline limit ~4 MB
_TIMEOUT = 15.0


def _guess_mime(filename: str) -> str:
    f = filename.lower()
    if f.endswith(".png"):
        return "image/png"
    if f.endswith(".webp"):
        return "image/webp"
    if f.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


async def describe_image(image_bytes: bytes, filename: str = "") -> str | None:
    """Return short Russian description of the image or None on failure."""
    if not config.gemini_api_key:
        return None
    if not image_bytes:
        return None
    if len(image_bytes) > _MAX_BYTES:
        logger.info("vision: skip oversized image (%d bytes) %s", len(image_bytes), filename)
        return None

    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": _PROMPT},
                {"inline_data": {"mime_type": _guess_mime(filename), "data": b64}},
            ],
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200},
    }

    try:
        async with LLM_SEMAPHORE:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _GEMINI_URL,
                    params={"key": config.gemini_api_key},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                ) as resp:
                    data = await resp.json()
    except Exception as exc:
        logger.warning("vision: request failed for %s: %s", filename, exc)
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        error = data.get("error") or {}
        msg = error.get("message") if isinstance(error, dict) else str(data)
        logger.warning("vision: no candidates for %s: %s", filename, msg)
        return None

    try:
        text = candidates[0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("vision: malformed response for %s", filename)
        return None

    return text or None

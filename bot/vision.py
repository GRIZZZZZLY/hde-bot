"""Vision-RAG: describe images via the Groq multimodal model for knowledge indexing."""
from __future__ import annotations

import base64
import logging

import aiohttp

from .config import config
from .hde_api import shared_session
from .llm_semaphore import LLM_SEMAPHORE

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

_PROMPT = (
    "Это скриншот из обращения в техподдержку кассового оборудования "
    "(АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).\n"
    "Прочитай текст ошибки/сообщения на картинке и кратко опиши проблему "
    "в 1–2 предложениях на русском. Если текста нет — опиши, что видно. "
    "Не добавляй вступлений и пояснений, только суть."
)

_MAX_BYTES = 4 * 1024 * 1024  # Groq base64 image limit ~4 MB
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
    if not config.groq_api_key:
        return None
    if not image_bytes:
        return None
    if len(image_bytes) > _MAX_BYTES:
        logger.info("vision: skip oversized image (%d bytes) %s", len(image_bytes), filename)
        return None

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{_guess_mime(filename)};base64,{b64}"
    payload = {
        "model": config.groq_vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 200,
    }
    if config.groq_reasoning_effort:
        payload["reasoning_effort"] = config.groq_reasoning_effort

    try:
        async with LLM_SEMAPHORE:
            async with shared_session() as session:
                async with session.post(
                    _GROQ_URL,
                    headers={"Authorization": f"Bearer {config.groq_api_key}"},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                ) as resp:
                    data = await resp.json()
    except Exception as exc:
        logger.warning("vision: request failed for %s: %s", filename, exc)
        return None

    choices = data.get("choices") or []
    if not choices:
        error = data.get("error") or {}
        msg = error.get("message") if isinstance(error, dict) else str(data)
        logger.warning("vision: no choices for %s: %s", filename, msg)
        return None

    try:
        text = (choices[0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("vision: malformed response for %s", filename)
        return None

    return text or None

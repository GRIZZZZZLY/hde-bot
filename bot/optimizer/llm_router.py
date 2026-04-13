"""Multi-LLM router for prompt mutation proposals."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

import aiohttp

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class GeminiClient:
    """Thin wrapper around Gemini generateContent API."""

    _URL_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent"
    )

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self._url = self._URL_TEMPLATE.format(model=model)

    async def complete(self, system: str, user: str) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1000},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url,
                params={"key": self.api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class GroqClient:
    """Thin wrapper around Groq OpenAI-compatible API."""

    _URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is not set — cannot use GroqClient")
        import httpx
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 1000,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


class LLMRouter:
    """Routes mutation requests to all available LLM clients in parallel."""

    def __init__(self, gemini_api_key: str, groq_api_key: str) -> None:
        self.clients: dict[str, LLMClient] = {
            "gemini": GeminiClient(model="gemini-2.5-flash", api_key=gemini_api_key),
            "llama": GroqClient(model="llama-3.3-70b-versatile", api_key=groq_api_key),
            "mixtral": GroqClient(model="mixtral-8x7b-32768", api_key=groq_api_key),
        }

    async def complete_all(self, system: str, user: str) -> dict[str, str]:
        """Call all clients in parallel. Skip clients that raise errors."""

        async def _safe_complete(name: str, client: LLMClient) -> tuple[str, str | None]:
            try:
                text = await client.complete(system, user)
                return name, text
            except Exception as exc:
                logger.warning("LLM client %s failed: %s", name, exc)
                return name, None

        tasks = [_safe_complete(name, client) for name, client in self.clients.items()]
        results = await asyncio.gather(*tasks)
        return {name: text for name, text in results if text is not None}

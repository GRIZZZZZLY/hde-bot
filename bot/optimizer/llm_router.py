"""Multi-LLM router for prompt mutation proposals."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

import aiohttp

from ..llm_semaphore import LLM_SEMAPHORE

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
        async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
            async with session.post(
                self._url,
                params={"key": self.api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
        candidates = data.get("candidates")
        if not candidates:
            error = data.get("error", {})
            msg = error.get("message") if isinstance(error, dict) else str(data)
            raise RuntimeError(f"Gemini returned no candidates: {msg}")
        return candidates[0]["content"]["parts"][0]["text"].strip()


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
        async with LLM_SEMAPHORE, httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


class OpenRouterClient:
    """Thin wrapper around OpenRouter OpenAI-compatible API."""

    _URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set — cannot use OpenRouterClient")
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
        async with LLM_SEMAPHORE, httpx.AsyncClient(timeout=30) as client:
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

    def __init__(
        self,
        gemini_api_key: str,
        groq_api_key: str,
        openrouter_api_key: str = "",
    ) -> None:
        self.clients: dict[str, LLMClient] = {
            "gemini": GeminiClient(model="gemini-2.5-flash", api_key=gemini_api_key),
            # Use smaller/faster Groq models for mutations to avoid rate limits.
            # Main AI summaries use llama-3.3-70b-versatile (separate quota).
            "llama": GroqClient(model="llama-3.1-8b-instant", api_key=groq_api_key),
            "gemma": GroqClient(model="gemma2-9b-it", api_key=groq_api_key),
        }
        if openrouter_api_key:
            self.clients["gemma4"] = OpenRouterClient(
                model="google/gemma-4-31b-it:free",
                api_key=openrouter_api_key,
            )

    async def complete_all(
        self, system: str, user: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Try Gemini first; fall back to Groq clients (in parallel) only if Gemini fails.

        Returns (results, errors) where:
          results: model_name → generated text (successful completions)
          errors:  model_name → error message (failed completions)
        """

        async def _safe_complete(
            name: str, client: LLMClient
        ) -> tuple[str, str | None, str | None]:
            try:
                text = await client.complete(system, user)
                return name, text, None
            except Exception as exc:
                logger.warning("LLM client %s failed: %s", name, exc)
                return name, None, str(exc)

        groq_clients = {k: v for k, v in self.clients.items() if k != "gemini"}
        gemini_client = self.clients.get("gemini")

        results: dict[str, str] = {}
        errors: dict[str, str] = {}

        # Gemini first — мутации самой умной модели. Groq остаётся резервом.
        if gemini_client is not None:
            name, text, err = await _safe_complete("gemini", gemini_client)
            if text is not None:
                results["gemini"] = text
            elif err is not None:
                errors["gemini"] = err

        # Fall back to Groq models (in parallel) only if Gemini failed
        if not results:
            groq_tasks = [_safe_complete(name, client) for name, client in groq_clients.items()]
            groq_results = await asyncio.gather(*groq_tasks)
            for name, text, err in groq_results:
                if text is not None:
                    results[name] = text
                elif err is not None:
                    errors[name] = err

        return results, errors

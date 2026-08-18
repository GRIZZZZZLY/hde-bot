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

    # Ключ OpenRouter-клиента в self.clients; пусто → фолбэка нет.
    fallback_name: str = ""

    def __init__(
        self,
        groq_api_key: str,
        openrouter_api_key: str = "",
        *,
        models: tuple[str, ...] | None = None,
        openrouter_model: str = "",
    ) -> None:
        # Модели — из env (OPTIMIZER_MUTATION_MODELS / OPENROUTER_MODEL): зашитый
        # в модуль id роняет весь прогон, когда провайдер снимает модель, и
        # правится только релизом. Разные семейства держим специально —
        # разнообразие мутаций плюс раздельные per-model квоты Groq.
        from ..config import config  # noqa: PLC0415 — избегаем цикла импорта

        if models is None:
            models = config.optimizer_mutation_models
        openrouter_model = openrouter_model or config.openrouter_model
        self.clients: dict[str, LLMClient] = {
            model: GroqClient(model=model, api_key=groq_api_key) for model in models
        }
        if openrouter_api_key:
            self.clients[openrouter_model] = OpenRouterClient(
                model=openrouter_model,
                api_key=openrouter_api_key,
            )
            self.fallback_name = openrouter_model

    async def complete_all(
        self, system: str, user: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Run all Groq clients in parallel; OpenRouter only if every Groq fails.

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

        groq_clients = {
            k: v for k, v in self.clients.items() if k != self.fallback_name
        }
        openrouter_client = (
            self.clients.get(self.fallback_name) if self.fallback_name else None
        )

        results: dict[str, str] = {}
        errors: dict[str, str] = {}

        groq_tasks = [_safe_complete(name, client) for name, client in groq_clients.items()]
        for name, text, err in await asyncio.gather(*groq_tasks):
            if text is not None:
                results[name] = text
            elif err is not None:
                errors[name] = err

        # OpenRouter — последний резерв, когда весь Groq недоступен
        if not results and openrouter_client is not None:
            name, text, err = await _safe_complete(self.fallback_name, openrouter_client)
            if text is not None:
                results[name] = text
            elif err is not None:
                errors[name] = err

        return results, errors

from __future__ import annotations

import asyncio
import logging

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3


class RetrySession(AiohttpSession):
    """AiohttpSession that automatically retries on Telegram rate limit (429)."""

    async def __call__(self, bot, method, timeout=None):
        for attempt in range(_MAX_RETRIES):
            try:
                return await super().__call__(bot, method, timeout=timeout)
            except TelegramRetryAfter as exc:
                if attempt >= _MAX_RETRIES - 1:
                    raise
                wait = exc.retry_after
                logger.warning(
                    "Rate limited by Telegram (%s), retrying in %ds (attempt %d/%d)",
                    method.__class__.__name__,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(wait)

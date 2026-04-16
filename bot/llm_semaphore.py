"""Shared asyncio.Semaphore for outbound LLM API calls (Gemini, Groq).

All modules that call Gemini / Groq must acquire this semaphore to prevent
rate-limit bursts and runaway concurrency during traffic spikes. The limit
(3 concurrent requests) reflects the bot's typical ticket-webhook rate plus
headroom for the nightly optimizer run.
"""
from __future__ import annotations

import asyncio

LLM_CONCURRENCY = 3
LLM_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

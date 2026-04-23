"""Rate limiting with Token Bucket algorithm to prevent 429 errors."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Dict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """Token bucket rate limiter.
    
    Allows burst of requests up to capacity, then limits to refill_rate per second.
    Prevents 429 Too Many Requests errors from APIs.
    """
    capacity: float          # Maximum tokens (burst size)
    refill_rate: float       # Tokens added per second
    tokens: float = field(default=None)
    last_update: float = field(default=None)
    
    def __post_init__(self):
        if self.tokens is None:
            self.tokens = self.capacity
        if self.last_update is None:
            self.last_update = time.monotonic()
    
    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_update = now
    
    async def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        """Try to acquire tokens. Returns True if successful, False if timeout.
        
        Args:
            tokens: Number of tokens to acquire
            timeout: Max seconds to wait (None = wait indefinitely)
        """
        start_time = time.monotonic()
        
        while True:
            self._refill()
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            
            # Calculate wait time for enough tokens
            needed = tokens - self.tokens
            wait_time = needed / self.refill_rate
            
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                remaining_timeout = timeout - elapsed
                if remaining_timeout <= 0:
                    logger.warning("Rate limit timeout after %.2fs", elapsed)
                    return False
                wait_time = min(wait_time, remaining_timeout)
            
            # Wait a bit before trying again
            await asyncio.sleep(min(wait_time, 0.1))
    
    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Try to acquire tokens without waiting. Returns True if successful."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class RateLimiter:
    """Multi-bucket rate limiter for different API endpoints/users."""
    
    def __init__(self):
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
    
    def get_bucket(
        self,
        key: str,
        capacity: float = 10.0,
        refill_rate: float = 2.0,
    ) -> TokenBucket:
        """Get or create a token bucket for a specific key."""
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(capacity=capacity, refill_rate=refill_rate)
        return self._buckets[key]
    
    async def acquire(
        self,
        key: str,
        tokens: float = 1.0,
        capacity: float = 10.0,
        refill_rate: float = 2.0,
        timeout: Optional[float] = None,
    ) -> bool:
        """Acquire tokens from a named bucket."""
        bucket = self.get_bucket(key, capacity, refill_rate)
        return await bucket.acquire(tokens, timeout)
    
    def try_acquire(
        self,
        key: str,
        tokens: float = 1.0,
        capacity: float = 10.0,
        refill_rate: float = 2.0,
    ) -> bool:
        """Try to acquire tokens without waiting."""
        bucket = self.get_bucket(key, capacity, refill_rate)
        return bucket.try_acquire(tokens)


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


# Pre-configured limiters for common use cases
async def limit_hde_api_call(timeout: Optional[float] = 30.0) -> bool:
    """Rate limit HDE API calls (default: 10 req/s, burst 20).
    
    HDE typically allows ~10-20 requests per second. Adjust based on actual limits.
    """
    limiter = get_rate_limiter()
    return await limiter.acquire(
        key="hde_api",
        tokens=1.0,
        capacity=20.0,
        refill_rate=10.0,
        timeout=timeout,
    )


async def limit_llm_call(timeout: Optional[float] = 60.0) -> bool:
    """Rate limit LLM API calls (default: 2 req/s, burst 5).
    
    LLM APIs often have stricter limits. Adjust based on your provider.
    """
    limiter = get_rate_limiter()
    return await limiter.acquire(
        key="llm_api",
        tokens=1.0,
        capacity=5.0,
        refill_rate=2.0,
        timeout=timeout,
    )


async def limit_telegram_bot(timeout: Optional[float] = 30.0) -> bool:
    """Rate limit Telegram Bot API calls (default: 30 req/s, burst 50).
    
    Telegram allows 30 messages per second. See: https://core.telegram.org/bots/faq
    """
    limiter = get_rate_limiter()
    return await limiter.acquire(
        key="telegram_bot",
        tokens=1.0,
        capacity=50.0,
        refill_rate=30.0,
        timeout=timeout,
    )

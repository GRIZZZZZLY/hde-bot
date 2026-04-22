"""LLM request caching to reduce token usage and improve response times."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional, Any, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cached LLM response with metadata."""
    response: str
    created_at: float
    ttl: float
    tokens_used: int
    model: str
    
    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return time.time() - self.created_at > self.ttl


class LLMCache:
    """Semantic cache for LLM responses.
    
    Caches LLM responses based on prompt hash to avoid redundant API calls.
    Can save 20-40% of token costs for repeated/similar queries.
    """
    
    def __init__(self, default_ttl: float = 3600.0, max_size: int = 1000):
        self._cache: Dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
    
    def _compute_key(self, prompt: str, model: str = "default") -> str:
        """Compute cache key from prompt and model."""
        key_data = f"{model}:{prompt}"
        return hashlib.sha256(key_data.encode()).hexdigest()
    
    async def get(self, prompt: str, model: str = "default") -> Optional[str]:
        """Get cached response if available and not expired."""
        key = self._compute_key(prompt, model)
        
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            
            if entry.is_expired():
                del self._cache[key]
                self._misses += 1
                logger.debug("Cache miss (expired): %s", key[:16])
                return None
            
            self._hits += 1
            logger.debug("Cache hit: %s", key[:16])
            return entry.response
    
    async def set(
        self,
        prompt: str,
        response: str,
        model: str = "default",
        ttl: Optional[float] = None,
        tokens_used: int = 0,
    ) -> None:
        """Cache an LLM response."""
        key = self._compute_key(prompt, model)
        
        async with self._lock:
            # Evict oldest entries if at capacity
            if len(self._cache) >= self._max_size:
                # Remove oldest 10% of entries
                sorted_entries = sorted(
                    self._cache.items(),
                    key=lambda x: x[1].created_at
                )
                evict_count = max(1, self._max_size // 10)
                for k, _ in sorted_entries[:evict_count]:
                    del self._cache[k]
                logger.debug("Evicted %d old cache entries", evict_count)
            
            self._cache[key] = CacheEntry(
                response=response,
                created_at=time.time(),
                ttl=ttl or self._default_ttl,
                tokens_used=tokens_used,
                model=model,
            )
            logger.debug("Cached response: %s", key[:16])
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0.0
        
        # Estimate token savings
        total_tokens_saved = sum(
            entry.tokens_used for entry in self._cache.values()
        )
        
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_percent": round(hit_rate, 2),
            "entries_count": len(self._cache),
            "max_size": self._max_size,
            "estimated_tokens_saved": total_tokens_saved,
        }
    
    async def clear(self) -> None:
        """Clear all cached entries."""
        async with self._lock:
            self._cache.clear()
            logger.info("LLM cache cleared")


# Global LLM cache instance
_llm_cache: Optional[LLMCache] = None


def get_llm_cache(default_ttl: float = 3600.0, max_size: int = 1000) -> LLMCache:
    """Get the global LLM cache instance."""
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = LLMCache(default_ttl=default_ttl, max_size=max_size)
    return _llm_cache


async def cached_llm_call(
    prompt: str,
    llm_func,
    model: str = "default",
    ttl: Optional[float] = None,
    **kwargs,
) -> tuple[str, bool]:
    """Call LLM with caching. Returns (response, was_cached).
    
    Args:
        prompt: The prompt to send to LLM
        llm_func: Async function that takes prompt and returns response string
        model: Model identifier for cache key
        ttl: Cache TTL in seconds (uses default if None)
        **kwargs: Additional args passed to llm_func
    
    Returns:
        Tuple of (response_text, was_cached_flag)
    """
    cache = get_llm_cache()
    
    # Try cache first
    cached_response = await cache.get(prompt, model)
    if cached_response is not None:
        return cached_response, True
    
    # Call LLM
    response = await llm_func(prompt, **kwargs)
    
    # Cache the response (best effort - don't fail if caching fails)
    try:
        # Estimate tokens (rough: ~4 chars per token)
        tokens_estimated = len(response) // 4
        await cache.set(prompt, response, model, ttl, tokens_estimated)
    except Exception as e:
        logger.warning("Failed to cache LLM response: %s", e)
    
    return response, False


# Specialized caches for different use cases
async def get_cached_summary(
    ticket_id: str,
    history_text: str,
    summarize_func,
) -> tuple[str, bool]:
    """Get cached AI summary for a ticket."""
    prompt = f"SUMMARY:{ticket_id}:{history_text}"
    return await cached_llm_call(
        prompt=prompt,
        llm_func=summarize_func,
        model="summary",
        ttl=7200.0,  # 2 hours - summaries stay relevant longer
    )


async def get_cached_categorization(
    text: str,
    categorize_func,
) -> tuple[str, bool]:
    """Get cached categorization for text."""
    prompt = f"CATEGORIZE:{text}"
    return await cached_llm_call(
        prompt=prompt,
        llm_func=categorize_func,
        model="categorization",
        ttl=86400.0,  # 24 hours - categories rarely change
    )


async def get_cached_embedding(
    text: str,
    embedding_func,
) -> tuple[Any, bool]:
    """Get cached embedding for text.
    
    Note: This caches the raw embedding vector, which can be large.
    Consider using a database for persistent embedding storage.
    """
    cache = get_llm_cache()
    key = f"EMBED:{hashlib.sha256(text.encode()).hexdigest()}"
    
    # For embeddings, we'd need to serialize/deserialize numpy arrays
    # This is a simplified version - in practice, use knowledge/store.py
    cached = await cache.get(key, model="embedding")
    if cached:
        import json
        return json.loads(cached), True
    
    embedding = await embedding_func(text)
    
    try:
        import json
        await cache.set(
            key,
            json.dumps(embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)),
            model="embedding",
            ttl=604800.0,  # 7 days - embeddings are stable
        )
    except Exception as e:
        logger.warning("Failed to cache embedding: %s", e)
    
    return embedding, False

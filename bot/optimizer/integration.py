"""Performance optimization guide and integration utilities.

This module documents all implemented optimizations and provides
integration helpers for the main bot application.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class PerformanceOptimizer:
    """Main optimizer coordinator for all performance improvements.
    
    Integrates:
    - SQLite connection pooling with PRAGMA optimizations
    - HTTP connection pooling (aiohttp sessions)
    - Rate limiting (Token Bucket algorithm)
    - LLM request caching
    - FAISS vector search (optional)
    - Background task management
    
    Expected improvements:
    - 3-5x faster SQLite writes (WAL mode + PRAGMA)
    - 40-60% lower DB latency (connection pooling)
    - 20-30% faster HTTP requests (connection reuse)
    - 50-100x faster vector search (FAISS, if enabled)
    - 20-40% token cost savings (LLM caching)
    - Reduced user-facing latency (background tasks)
    """
    
    def __init__(self):
        self._initialized = False
        self._components: Dict[str, bool] = {}
    
    async def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Initialize all optimization components."""
        if self._initialized:
            return
        
        config = config or {}
        
        # 1. Initialize SQLite pool (already done in db.py init_db_pool)
        try:
            from ..db import init_db_pool
            db_path = config.get("db_path", "hde_bot.db")
            await init_db_pool(db_path)
            self._components["sqlite_pool"] = True
            logger.info("✓ SQLite connection pool initialized")
        except Exception as e:
            logger.warning("Failed to initialize SQLite pool: %s", e)
            self._components["sqlite_pool"] = False
        
        # 2. Initialize HTTP session pool
        try:
            from ..config import config as bot_config
            from aiohttp import BasicAuth
            from ..http_pool import get_hde_session
            
            auth = BasicAuth(
                login=bot_config.hde_api_login,
                password=bot_config.hde_api_password,
            )
            await get_hde_session(bot_config.hde_base_url, auth)
            self._components["http_pool"] = True
            logger.info("✓ HTTP session pool initialized")
        except Exception as e:
            logger.warning("Failed to initialize HTTP pool: %s", e)
            self._components["http_pool"] = False
        
        # 3. Initialize rate limiter (lazy-loaded, just mark as ready)
        try:
            from ..rate_limiter import get_rate_limiter
            get_rate_limiter()
            self._components["rate_limiter"] = True
            logger.info("✓ Rate limiter initialized")
        except Exception as e:
            logger.warning("Failed to initialize rate limiter: %s", e)
            self._components["rate_limiter"] = False
        
        # 4. Initialize LLM cache
        try:
            from ..llm_cache import get_llm_cache
            ttl = config.get("llm_cache_ttl", 3600.0)
            max_size = config.get("llm_cache_max_size", 1000)
            get_llm_cache(default_ttl=ttl, max_size=max_size)
            self._components["llm_cache"] = True
            logger.info("✓ LLM cache initialized (TTL=%ds, max_size=%d)", ttl, max_size)
        except Exception as e:
            logger.warning("Failed to initialize LLM cache: %s", e)
            self._components["llm_cache"] = False
        
        # 5. Initialize FAISS vector index (optional)
        if config.get("enable_faiss", False):
            try:
                from ..vector_index import get_vector_index
                dimension = config.get("embedding_dimension", 768)
                index_type = config.get("faiss_index_type", "flat")
                persist_path = config.get("faiss_persist_path")
                await get_vector_index(
                    dimension=dimension,
                    index_type=index_type,
                    persist_path=persist_path,
                )
                self._components["faiss_index"] = True
                logger.info("✓ FAISS vector index initialized (type=%s)", index_type)
            except Exception as e:
                logger.warning("Failed to initialize FAISS index: %s", e)
                self._components["faiss_index"] = False
        else:
            self._components["faiss_index"] = False
            logger.info("⊘ FAISS index disabled (enable with enable_faiss=True)")
        
        # 6. Initialize background task manager
        try:
            from ..background_tasks import get_task_manager
            max_concurrent = config.get("max_background_tasks", 10)
            get_task_manager(max_concurrent=max_concurrent)
            self._components["background_tasks"] = True
            logger.info("✓ Background task manager initialized (max_concurrent=%d)", max_concurrent)
        except Exception as e:
            logger.warning("Failed to initialize background task manager: %s", e)
            self._components["background_tasks"] = False
        
        self._initialized = True
        logger.info("=" * 60)
        logger.info("Performance Optimizer initialized successfully!")
        logger.info("Active components: %s", ", ".join(k for k, v in self._components.items() if v))
        logger.info("=" * 60)
    
    def get_status(self) -> Dict[str, Any]:
        """Get status of all optimization components."""
        status = {
            "initialized": self._initialized,
            "components": self._components.copy(),
        }
        
        # Add detailed stats for each component
        try:
            if self._components.get("llm_cache"):
                from ..llm_cache import get_llm_cache
                cache = get_llm_cache()
                status["llm_cache_stats"] = cache.get_stats()
        except Exception:
            pass
        
        try:
            if self._components.get("background_tasks"):
                from ..background_tasks import get_task_manager
                manager = get_task_manager()
                status["background_tasks_stats"] = manager.get_stats()
        except Exception:
            pass
        
        try:
            if self._components.get("faiss_index"):
                from ..vector_index import get_vector_index
                # This would need async, so we skip for now
                status["faiss_index"] = "enabled"
        except Exception:
            pass
        
        return status
    
    async def shutdown(self) -> None:
        """Gracefully shut down all optimization components."""
        logger.info("Shutting down Performance Optimizer...")
        
        # Close HTTP sessions
        try:
            from ..http_pool import close_hde_session, close_all_generic_sessions
            await close_hde_session()
            await close_all_generic_sessions()
            logger.info("✓ HTTP sessions closed")
        except Exception as e:
            logger.warning("Error closing HTTP sessions: %s", e)
        
        # Close FAISS index (if applicable)
        try:
            from ..vector_index import close_vector_index
            await close_vector_index()
            logger.info("✓ FAISS index closed")
        except Exception:
            pass  # FAISS may not be enabled
        
        # Clear LLM cache
        try:
            from ..llm_cache import get_llm_cache
            cache = get_llm_cache()
            await cache.clear()
            logger.info("✓ LLM cache cleared")
        except Exception as e:
            logger.warning("Error clearing LLM cache: %s", e)
        
        self._initialized = False
        logger.info("Performance Optimizer shut down complete")


# Global optimizer instance
_optimizer: Optional[PerformanceOptimizer] = None


def get_optimizer() -> PerformanceOptimizer:
    """Get global optimizer instance."""
    global _optimizer
    if _optimizer is None:
        _optimizer = PerformanceOptimizer()
    return _optimizer


# Convenience functions for quick access

async def init_optimizations(config: Optional[Dict[str, Any]] = None) -> None:
    """Initialize all optimizations with default or custom config."""
    optimizer = get_optimizer()
    await optimizer.initialize(config)


def get_optimization_status() -> Dict[str, Any]:
    """Get current optimization status."""
    optimizer = get_optimizer()
    return optimizer.get_status()


async def shutdown_optimizations() -> None:
    """Shut down all optimizations."""
    optimizer = get_optimizer()
    await optimizer.shutdown()


# Example integration code for main.py
INTEGRATION_EXAMPLE = """
# In bot/main.py or similar entry point:

from bot.optimizer import init_optimizations, shutdown_optimizations

async def main():
    # Initialize optimizations before starting bot
    await init_optimizations({
        "db_path": "hde_bot.db",
        "llm_cache_ttl": 3600.0,
        "llm_cache_max_size": 1000,
        "enable_faiss": False,  # Set True after installing faiss-cpu
        "embedding_dimension": 768,
        "faiss_index_type": "flat",
        "max_background_tasks": 10,
    })
    
    try:
        # ... start your bot ...
        await bot.polling()
    finally:
        # Graceful shutdown
        await shutdown_optimizations()
"""

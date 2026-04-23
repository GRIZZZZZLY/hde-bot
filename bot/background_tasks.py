"""Background task utilities for non-blocking operations.

This module provides helpers for running expensive operations in the background
to reduce user-facing latency.

Benefits:
- Faster response times for user requests
- Better resource utilization
- Graceful handling of long-running tasks
- Automatic error handling and logging
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Callable, Any, Awaitable, Dict
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Result of a background task execution."""
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class BackgroundTaskManager:
    """Manages background tasks with tracking and error handling."""
    
    def __init__(self, max_concurrent: int = 10):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, TaskResult] = {}
        self._lock = asyncio.Lock()
    
    async def submit(
        self,
        task_id: str,
        coro_func: Callable[..., Awaitable[Any]],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> bool:
        """Submit a background task. Returns True if accepted, False if already running."""
        async with self._lock:
            if task_id in self._tasks and not self._tasks[task_id].done():
                logger.debug("Task %s already running, skipping", task_id)
                return False
        
        async def wrapper():
            start_time = time.time()
            started_at = datetime.now(timezone.utc)
            
            try:
                async with self._semaphore:
                    result = await asyncio.wait_for(coro_func(*args, **kwargs), timeout=timeout)
                
                duration_ms = (time.time() - start_time) * 1000
                completed_at = datetime.now(timezone.utc)
                
                task_result = TaskResult(
                    success=True,
                    result=result,
                    duration_ms=duration_ms,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                
                async with self._lock:
                    self._results[task_id] = task_result
                    self._tasks.pop(task_id, None)
                
                logger.debug("Task %s completed in %.2fms", task_id, duration_ms)
                return result
                
            except asyncio.TimeoutError:
                error_msg = f"Task {task_id} timed out after {timeout}s"
                logger.warning(error_msg)
                async with self._lock:
                    self._results[task_id] = TaskResult(
                        success=False,
                        error=error_msg,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                    )
                    self._tasks.pop(task_id, None)
                raise
                
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                error_msg = f"Task {task_id} failed: {type(e).__name__}: {e}"
                logger.error(error_msg, exc_info=True)
                
                async with self._lock:
                    self._results[task_id] = TaskResult(
                        success=False,
                        error=error_msg,
                        duration_ms=duration_ms,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                    )
                    self._tasks.pop(task_id, None)
                raise
        
        async with self._lock:
            task = asyncio.create_task(wrapper())
            self._tasks[task_id] = task
        
        return True
    
    def get_result(self, task_id: str) -> Optional[TaskResult]:
        """Get result of a completed task."""
        return self._results.get(task_id)
    
    def is_running(self, task_id: str) -> bool:
        """Check if a task is currently running."""
        task = self._tasks.get(task_id)
        return task is not None and not task.done()
    
    async def wait(self, task_id: str, timeout: Optional[float] = None) -> Optional[TaskResult]:
        """Wait for a task to complete and return its result."""
        task = self._tasks.get(task_id)
        if task is None:
            return self._results.get(task_id)
        
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            return self._results.get(task_id)
        except asyncio.TimeoutError:
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get task manager statistics."""
        running = sum(1 for t in self._tasks.values() if not t.done())
        completed = len([r for r in self._results.values() if r.success])
        failed = len([r for r in self._results.values() if not r.success])
        
        return {
            "running_tasks": running,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "total_tracked": len(self._tasks) + len(self._results),
        }
    
    async def cleanup_old_results(self, max_age_seconds: float = 3600.0) -> int:
        """Remove old results to prevent memory bloat. Returns count removed."""
        now = datetime.now(timezone.utc)
        removed = 0
        
        async with self._lock:
            to_remove = []
            for task_id, result in self._results.items():
                if result.completed_at:
                    age = (now - result.completed_at).total_seconds()
                    if age > max_age_seconds:
                        to_remove.append(task_id)
            
            for task_id in to_remove:
                del self._results[task_id]
                removed += 1
        
        if removed > 0:
            logger.debug("Cleaned up %d old task results", removed)
        
        return removed


# Global background task manager
_task_manager: Optional[BackgroundTaskManager] = None


def get_task_manager(max_concurrent: int = 10) -> BackgroundTaskManager:
    """Get global background task manager."""
    global _task_manager
    if _task_manager is None:
        _task_manager = BackgroundTaskManager(max_concurrent=max_concurrent)
    return _task_manager


# Convenience decorators and helpers
def run_background(
    task_id: str,
    timeout: Optional[float] = None,
    ignore_errors: bool = False,
):
    """Decorator to run async function in background.
    
    Usage:
        @run_background("my_task", timeout=30.0)
        async def my_expensive_function(data):
            ...
    """
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[bool]]:
        async def wrapper(*args, **kwargs) -> bool:
            manager = get_task_manager()
            try:
                return await manager.submit(
                    task_id=task_id,
                    coro_func=func,
                    *args,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception as e:
                if ignore_errors:
                    logger.warning("Background task %s failed silently: %s", task_id, e)
                    return False
                raise
        return wrapper
    return decorator


async def fire_and_forget(
    coro_func: Callable[..., Awaitable[Any]],
    *args,
    task_name: str = "unnamed",
    **kwargs,
) -> None:
    """Run a coroutine in the background without tracking.
    
    Use for truly fire-and-forget tasks where you don't need results.
    Errors are logged but not raised.
    """
    async def wrapper():
        try:
            await coro_func(*args, **kwargs)
        except Exception as e:
            logger.error("Fire-and-forget task %s failed: %s", task_name, e, exc_info=True)
    
    asyncio.create_task(wrapper())


# Specialized background operations for common bot tasks

async def background_llm_summarization(
    topic_id: int,
    ticket_id: str,
    history_text: str,
    summarize_func: Callable[..., Awaitable[str]],
) -> Optional[str]:
    """Run LLM summarization in background, return immediately with placeholder."""
    task_id = f"summary_{topic_id}"
    manager = get_task_manager()
    
    async def do_summarize():
        summary = await summarize_func(history_text)
        # Here you would post the summary to Telegram
        logger.info("Summary generated for topic %d", topic_id)
        return summary
    
    submitted = await manager.submit(
        task_id=task_id,
        coro_func=do_summarize,
        timeout=120.0,  # 2 minute timeout for LLM
    )
    
    if submitted:
        logger.info("Background summarization started for topic %d", topic_id)
        return "Generating summary..."  # Immediate placeholder response
    else:
        logger.warning("Summarization already in progress for topic %d", topic_id)
        return "Summary generation in progress..."


async def background_embedding_generation(
    item_id: int,
    content: str,
    embed_func: Callable[..., Awaitable[Any]],
) -> bool:
    """Generate embeddings in background without blocking."""
    task_id = f"embed_{item_id}"
    manager = get_task_manager()
    
    async def do_embed():
        embedding = await embed_func(content)
        # Here you would save to database
        logger.info("Embedding generated for item %d", item_id)
        return embedding
    
    return await manager.submit(
        task_id=task_id,
        coro_func=do_embed,
        timeout=60.0,
    )


async def background_hde_sync(
    ticket_id: str,
    sync_func: Callable[..., Awaitable[Any]],
) -> bool:
    """Sync with HDE API in background."""
    task_id = f"hde_sync_{ticket_id}"
    manager = get_task_manager()
    
    return await manager.submit(
        task_id=task_id,
        coro_func=sync_func,
        timeout=30.0,
    )

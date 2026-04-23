"""Performance optimizer module.

This module provides tools for monitoring and optimizing bot performance:
- HTTP connection pooling
- Rate limiting
- LLM caching
- Background task management
- Vector search with FAISS
"""

from .agent import OptimizerAgent
from .evaluator import PerformanceEvaluator
from .llm_router import LLMRouter, get_llm_router
from .mutations import MutationEngine

__all__ = [
    "OptimizerAgent",
    "PerformanceEvaluator", 
    "LLMRouter",
    "get_llm_router",
    "MutationEngine",
]

"""In-memory ops counters exposed via /health. Reset on restart by design."""
from __future__ import annotations

import time
from collections import Counter, deque

_counters: Counter[str] = Counter()
_llm_latencies: deque[float] = deque(maxlen=100)
_started_at = time.monotonic()


def inc(name: str, value: int = 1) -> None:
    _counters[name] += value


def observe_llm_latency(seconds: float) -> None:
    _llm_latencies.append(seconds)


def snapshot() -> dict:
    data: dict = dict(_counters)
    data["uptime_seconds"] = int(time.monotonic() - _started_at)
    if _llm_latencies:
        data["llm_latency_avg_ms"] = int(sum(_llm_latencies) / len(_llm_latencies) * 1000)
    return data


def reset() -> None:
    """Test helper."""
    _counters.clear()
    _llm_latencies.clear()

"""Профиль голоса оператора: реальные ответы клиентам как образец стиля.

Источник — optimization_samples.op_answer (тексты, которые оператор сам
писал/правил через кнопку ✏️). Скрипт scripts/build_voice_profile.py
собирает примеры в bot/prompts/voice_profile.json; ai_summary подмешивает
их в системный промт.
"""
from __future__ import annotations

import json
from pathlib import Path

_PROFILE_PATH = Path(__file__).parent / "prompts" / "voice_profile.json"
_MIN_LEN = 40
_MAX_LEN = 600


def extract_voice_examples(texts: list[str], max_examples: int = 8) -> list[str]:
    """Отбирает характерные ответы: нормализует пробелы, режет по длине, дедупит."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        t = " ".join((text or "").split())
        if not (_MIN_LEN <= len(t) <= _MAX_LEN):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= max_examples:
            break
    return out


def load_voice_examples() -> list[str]:
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        return [e for e in data.get("examples", []) if isinstance(e, str)]
    except Exception:
        return []

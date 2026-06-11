"""Детерминированный train/holdout split для optimization_samples.

Holdout стабилен: один и тот же id всегда попадает в одну корзину,
независимо от размера выборки и порядка. Это лечит переобучение
оптимизатора: apply-решение принимается по сэмплам, на которых
мутации не настраивались.
"""
from __future__ import annotations

import hashlib

HOLDOUT_RATIO = 0.2


def is_holdout(sample_id: int | str, ratio: float = HOLDOUT_RATIO) -> bool:
    digest = hashlib.sha1(str(sample_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:2], "big") / 65535.0
    return bucket < ratio


def split_samples(
    samples: list[dict], ratio: float = HOLDOUT_RATIO
) -> tuple[list[dict], list[dict]]:
    """Returns (train, holdout)."""
    train: list[dict] = []
    holdout: list[dict] = []
    for s in samples:
        key = s.get("id", s.get("ticket_id", ""))
        (holdout if is_holdout(key, ratio) else train).append(s)
    return train, holdout

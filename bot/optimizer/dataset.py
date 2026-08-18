"""Детерминированный train/holdout split и защита независимости наборов.

Holdout стабилен: одна и та же корзина независимо от размера выборки и порядка.
Это лечит переобучение оптимизатора — apply-решение принимается по сэмплам, на
которых мутации не настраивались.

Корзину определяет ТИКЕТ, а не отдельный сэмпл. У одного тикета бывает
несколько предложений (trigger_source: first / button / reply) с почти
одинаковой историей; по-сэмпловый сплит разводил их по train и holdout, и
прирост кандидата на holdout мог быть переобучением на своих же данных.

Golden set — третий, независимый набор (audit): его тикеты исключаются и из
train, и из holdout. Иначе набор, по которому принимается финальное решение,
постепенно превращается в ещё один validation.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

HOLDOUT_RATIO = 0.2

# Пишется scripts/golden_set.py freeze рядом с самим снапшотом.
GOLDEN_TICKETS_PATH = "artifacts/golden/golden_tickets.txt"


def is_holdout(sample_id: int | str, ratio: float = HOLDOUT_RATIO) -> bool:
    digest = hashlib.sha1(str(sample_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:2], "big") / 65535.0
    return bucket < ratio


def group_key(sample: dict) -> str:
    """Ключ корзины: ticket_id, иначе id сэмпла.

    Фолбэк нужен для старых выборок из optimization_samples, где тикет мог быть
    не заполнен, и для синтетических наборов в тестах.
    """
    for key in ("ticket_id", "id"):
        value = sample.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def split_samples(
    samples: list[dict], ratio: float = HOLDOUT_RATIO
) -> tuple[list[dict], list[dict]]:
    """Returns (train, holdout). Все сэмплы одного тикета — в одной корзине."""
    train: list[dict] = []
    holdout: list[dict] = []
    for s in samples:
        (holdout if is_holdout(group_key(s), ratio) else train).append(s)
    return train, holdout


def load_golden_ticket_ids(path: str = GOLDEN_TICKETS_PATH) -> set[str]:
    """ID тикетов golden set. Файла нет (машина, где golden не собирали) →
    пустое множество: это не ошибка, просто нечего исключать."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def exclude_tickets(samples: list[dict], ticket_ids) -> list[dict]:
    """Убирает сэмплы перечисленных тикетов (golden set)."""
    if not ticket_ids:
        return list(samples)
    blocked = {str(t) for t in ticket_ids}
    return [s for s in samples if str(s.get("ticket_id", "")) not in blocked]

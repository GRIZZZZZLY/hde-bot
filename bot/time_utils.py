from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

STORAGE_FORMAT = "%Y-%m-%d %H:%M:%S"
INPUT_FORMATS = (
    STORAGE_FORMAT,
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_storage(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(STORAGE_FORMAT)


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    for fmt in INPUT_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

"""
Work schedule guard: suppresses all bot notifications outside working hours.

Configuration (via config.py / env):
  WORK_DAYS        comma-separated Python weekday numbers (0=Mon … 6=Sun)
                   default: 0,1,2,3,6  (Mon–Thu + Sun)
  WORK_HOUR_START  start hour in Europe/Moscow, inclusive  (default: 9)
  WORK_HOUR_END    end hour in Europe/Moscow, exclusive    (default: 18)
"""
from __future__ import annotations

import logging
import zoneinfo
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_UTC = timezone.utc
_MSK = zoneinfo.ZoneInfo("Europe/Moscow")

# In-process vacation state (resets on restart — intentional for simplicity)
_vacation_until: datetime | None = None  # timezone-aware UTC


# ── public API ────────────────────────────────────────────────────────────────

def _today_msk():
    """Today's date in Moscow time."""
    return datetime.now(_UTC).astimezone(_MSK).date()


def is_work_day() -> bool:
    """Return True if today is a scheduled work day (ignores the hour)."""
    from .config import config

    now_utc = datetime.now(_UTC)
    if _vacation_until is not None and now_utc < _vacation_until:
        return False

    return now_utc.astimezone(_MSK).weekday() in config.work_days


def was_yesterday_work_day() -> bool:
    """Return True if yesterday (MSK) was a scheduled work day."""
    from .config import config

    yesterday_msk = (datetime.now(_UTC).astimezone(_MSK) - timedelta(days=1))
    return yesterday_msk.weekday() in config.work_days


def last_work_day():
    """Return the most recent past work day (MSK) as a date. Never returns today.

    On Monday returns Friday (or Thursday if Friday is not a work day).
    Useful for the daily report: finds the last day there was actual work.
    """
    from .config import config
    from datetime import date as _date

    candidate = (datetime.now(_UTC).astimezone(_MSK) - timedelta(days=1)).date()
    for _ in range(14):
        if candidate.weekday() in config.work_days:
            return candidate
        candidate -= timedelta(days=1)
    # fallback — should never reach here with a sane work_days config
    return (datetime.now(_UTC).astimezone(_MSK) - timedelta(days=1)).date()


def is_work_time() -> bool:
    """Return True when the bot should send real-time notifications (day AND hour)."""
    from .config import config

    if not is_work_day():
        return False

    now_msk = datetime.now(_UTC).astimezone(_MSK)
    return config.work_hour_start <= now_msk.hour < config.work_hour_end


def set_vacation(until: datetime | None) -> None:
    """Enable vacation mode until *until* (UTC-aware) or disable if None."""
    global _vacation_until
    _vacation_until = until
    if until:
        logger.info("Vacation mode ON until %s UTC", until.strftime("%Y-%m-%d %H:%M"))
    else:
        logger.info("Vacation mode OFF")


def is_on_vacation() -> bool:
    """Return True if vacation mode is currently active."""
    if _vacation_until is None:
        return False
    return datetime.now(_UTC) < _vacation_until


def vacation_until() -> datetime | None:
    """Return the UTC datetime until which vacation is active, or None."""
    return _vacation_until


# ── persisted vacation window ────────────────────────────────────────────────
#
# set_vacation() only guards *today* — it says nothing about which past dates
# were days off. The daily report needs that: HDE omits an operator with zero
# closed tickets from the staff report entirely, so asking for a vacation day
# fails with "Operator ... not found in staff report". The window also survives
# a restart, which the in-process flag does not.

_KEY_UNTIL = "vacation_until_utc"
_KEY_START = "vacation_start_day"
_KEY_END = "vacation_end_day"


async def enable_vacation(until: datetime) -> None:
    """Turn on vacation mode and persist it: resume time + the days off."""
    from .db import set_setting

    set_vacation(until)
    start = _today_msk()
    end = until.astimezone(_MSK).date() - timedelta(days=1)
    await set_setting(_KEY_UNTIL, until.isoformat())
    await set_setting(_KEY_START, start.isoformat())
    await set_setting(_KEY_END, end.isoformat() if end >= start else "")


async def disable_vacation() -> None:
    """Turn off vacation mode early, keeping the days already taken off."""
    from .db import set_setting

    set_vacation(None)
    await set_setting(_KEY_UNTIL, "")
    start_raw = await _get(_KEY_START)
    end = _today_msk() - timedelta(days=1)
    if start_raw and date.fromisoformat(start_raw) <= end:
        await set_setting(_KEY_END, end.isoformat())
    else:
        await set_setting(_KEY_START, "")
        await set_setting(_KEY_END, "")


async def is_vacation_day(day) -> bool:
    """Return True if *day* falls inside the recorded vacation window."""
    start_raw = await _get(_KEY_START)
    end_raw = await _get(_KEY_END)
    if not start_raw or not end_raw:
        return False
    return date.fromisoformat(start_raw) <= day <= date.fromisoformat(end_raw)


async def restore_vacation() -> None:
    """Re-arm in-memory vacation state from the DB (call on startup)."""
    raw = await _get(_KEY_UNTIL)
    if not raw:
        return
    until = datetime.fromisoformat(raw)
    if until > datetime.now(_UTC):
        set_vacation(until)


async def _get(key: str) -> str:
    from .db import get_setting

    return await get_setting(key, "")


def next_work_start() -> datetime:
    """Return the next work-day start time as a UTC-aware datetime."""
    from .config import config

    candidate = datetime.now(_MSK).replace(minute=0, second=0, microsecond=0)
    # If we're before start today, today might still qualify
    if candidate.hour < config.work_hour_start:
        candidate = candidate.replace(hour=config.work_hour_start)
    else:
        candidate = candidate.replace(hour=config.work_hour_start) + timedelta(days=1)

    # Walk forward until we hit a work day
    for _ in range(14):  # safety: max 2 weeks
        if candidate.weekday() in config.work_days:
            return candidate.astimezone(_UTC)
        candidate += timedelta(days=1)

    # Fallback: 7 days from now
    return (datetime.now(_UTC) + timedelta(days=7))

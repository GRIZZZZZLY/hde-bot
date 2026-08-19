"""Миграции колонок без слепого except + суточный снапшот базы.

Раньше каждая миграция колонки стояла в `try/except Exception: pass` с
комментарием «column already exists»: занятая база, битый файл и кончившееся
место выглядели ровно как «колонка уже есть», бот стартовал на неполной схеме.
Плюс копии базы не делались вовсе, а схема нигде не версионируется.
"""
import sqlite3

import aiosqlite
import pytest

import bot.db as db_module
from bot.config import config as _cfg
from bot.db.backup import backup_database, backup_name, list_backups, prune_backups
from bot.db.core import _add_column_if_missing, _table_exists, connect


# --- миграции колонок ---

@pytest.mark.asyncio
async def test_add_column_adds_once_and_reports():
    await db_module.init_db()
    async with connect() as db:
        added = await _add_column_if_missing(db, "ticket_topics", "probe_col", "TEXT")
        again = await _add_column_if_missing(db, "ticket_topics", "probe_col", "TEXT")
        await db.commit()
    assert added is True
    assert again is False  # второй старт — no-op, но не пойманное исключение


@pytest.mark.asyncio
async def test_add_column_propagates_real_error():
    """Ошибка, которая раньше выглядела как «колонка уже есть», теперь летит наверх."""
    await db_module.init_db()
    async with connect() as db:
        with pytest.raises(aiosqlite.Error):
            await _add_column_if_missing(db, "no_such_table", "col", "TEXT")


@pytest.mark.asyncio
async def test_table_exists():
    await db_module.init_db()
    async with connect() as db:
        assert await _table_exists(db, "ticket_topics") is True
        assert await _table_exists(db, "nope") is False


@pytest.mark.asyncio
async def test_init_db_twice_is_idempotent():
    """Второй старт на существующей базе не падает и схему не портит."""
    await db_module.init_db()
    await db_module.init_db()
    async with connect() as db:
        assert await _table_exists(db, "ai_suggestions") is True
        async with db.execute("PRAGMA table_info(ai_suggestions)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
    assert {"draft_answer", "judge_category"} <= cols


# --- снапшот ---

@pytest.mark.asyncio
async def test_backup_creates_readable_snapshot(tmp_path):
    await db_module.init_db()
    async with connect() as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('probe', 'значение')"
        )
        await db.commit()

    path = await backup_database(tmp_path, keep=3)

    assert path.exists()
    # Снапшот должен быть валидной базой с теми же данными, а не куском файла
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM bot_settings WHERE key='probe'").fetchone()
    conn.close()
    assert row[0] == "значение"


@pytest.mark.asyncio
async def test_backup_same_day_returns_existing(tmp_path):
    """Второй вызов в те же сутки не падает: VACUUM INTO не пишет в существующий файл."""
    await db_module.init_db()
    first = await backup_database(tmp_path, keep=3)
    stamp = first.stat().st_mtime_ns
    second = await backup_database(tmp_path, keep=3)
    assert second == first
    assert second.stat().st_mtime_ns == stamp  # файл не перезаписан


@pytest.mark.asyncio
async def test_backup_prunes_before_snapshot(tmp_path):
    """Место освобождают старые копии, поэтому чистка идёт ДО снятия новой."""
    await db_module.init_db()
    for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
        (tmp_path / f"hde_bot-{day}.db").write_bytes(b"stale")

    await backup_database(tmp_path, keep=2)

    names = [p.name for p in list_backups(tmp_path)]
    from datetime import datetime, timezone
    today = backup_name(datetime.now(timezone.utc))
    assert names == ["hde_bot-2026-08-12.db", today]


@pytest.mark.asyncio
async def test_backup_refuses_without_free_space(tmp_path, monkeypatch):
    """Заполненный диск останавливает запись в SQLite — это хуже, чем нет копии."""
    import bot.db.backup as backup_module
    await db_module.init_db()

    class _Usage:
        total = used = 0
        free = 0

    monkeypatch.setattr(backup_module.shutil, "disk_usage", lambda _p: _Usage())

    with pytest.raises(RuntimeError, match="недостаточно места"):
        await backup_database(tmp_path, keep=3)
    assert list_backups(tmp_path) == []


def test_prune_keeps_newest(tmp_path):
    for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
        (tmp_path / f"hde_bot-{day}.db").write_bytes(b"x")
    removed = prune_backups(tmp_path, keep=2)
    assert [p.name for p in removed] == ["hde_bot-2026-08-01.db", "hde_bot-2026-08-02.db"]
    assert [p.name for p in list_backups(tmp_path)] == [
        "hde_bot-2026-08-03.db", "hde_bot-2026-08-04.db",
    ]


def test_prune_ignores_foreign_files(tmp_path):
    (tmp_path / "hde_bot-2026-08-01.db").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("не наш файл", encoding="utf-8")
    prune_backups(tmp_path, keep=0)
    assert (tmp_path / "notes.txt").exists()
    assert list_backups(tmp_path) == []


def test_list_backups_missing_dir_is_empty(tmp_path):
    assert list_backups(tmp_path / "nope") == []


# --- планировщик ---

@pytest.mark.asyncio
async def test_scheduler_backs_up_once_a_day(tmp_path, monkeypatch):
    from bot import scheduler

    monkeypatch.setattr(scheduler, "_last_db_backup_date", None)
    monkeypatch.setattr(_cfg, "db_backup_dir", str(tmp_path))
    monkeypatch.setattr(_cfg, "db_backup_hour_utc", 1)
    calls = []

    async def fake_backup():
        calls.append(1)
        return tmp_path / "snap.db"

    import bot.db.backup as backup_module
    monkeypatch.setattr(backup_module, "backup_database", fake_backup)

    class _FrozenNow:
        @staticmethod
        def now(tz=None):
            from datetime import datetime as _dt
            return _dt(2026, 8, 19, 1, 5, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", _FrozenNow)

    class _Bot:
        async def send_message(self, *a, **kw):
            raise AssertionError("успешный бэкап оператора не беспокоит")

    await scheduler._maybe_backup_db(_Bot())
    await scheduler._maybe_backup_db(_Bot())

    assert calls == [1]  # второй проход в тот же час — no-op


@pytest.mark.asyncio
async def test_scheduler_reports_backup_failure(tmp_path, monkeypatch):
    """Молча не сделанный бэкап бесполезен — оператор должен узнать."""
    from bot import scheduler

    monkeypatch.setattr(scheduler, "_last_db_backup_date", None)
    monkeypatch.setattr(_cfg, "db_backup_dir", str(tmp_path))
    monkeypatch.setattr(_cfg, "db_backup_hour_utc", 1)

    async def failing_backup():
        raise RuntimeError("недостаточно места")

    import bot.db.backup as backup_module
    monkeypatch.setattr(backup_module, "backup_database", failing_backup)

    class _FrozenNow:
        @staticmethod
        def now(tz=None):
            from datetime import datetime as _dt
            return _dt(2026, 8, 19, 1, 5, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", _FrozenNow)

    sent = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    await scheduler._maybe_backup_db(_Bot())

    assert len(sent) == 1
    assert "недостаточно места" in sent[0]


@pytest.mark.asyncio
async def test_scheduler_backup_disabled_by_empty_dir(monkeypatch):
    from bot import scheduler

    monkeypatch.setattr(scheduler, "_last_db_backup_date", None)
    monkeypatch.setattr(_cfg, "db_backup_dir", "")

    async def boom():  # pragma: no cover — не должен вызываться
        raise AssertionError("backup must not run when disabled")

    import bot.db.backup as backup_module
    monkeypatch.setattr(backup_module, "backup_database", boom)

    await scheduler._maybe_backup_db(object())

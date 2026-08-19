"""Суточный снапшот SQLite.

Схема нигде не версионируется — миграции это идемпотентные ALTER на старте
(см. `_add_column_if_missing`), инсталляция одна, откат релиза откатывает код,
но не данные. Единственная защита от битой миграции или неудачного релиза —
свежая копия базы.

Файл базы НЕЛЬЗЯ копировать через `cp` на живой системе: страницы попадут в
копию в разорванном состоянии, а журнал/WAL — не согласованно с ними, и «бэкап»
окажется мусором ровно тогда, когда понадобится. `VACUUM INTO` делает
согласованный снапшот силами самого SQLite и попутно сжимает файл.

Место на диске здесь не абстракция: прод-база 242 МБ, и доминируют в ней
эмбеддинги float32 (`dialogue_pairs`, `knowledge_items`) — они почти не
сжимаются. Поэтому копий держим три, старые удаляем ДО снятия новой, и если
после этого свободного места меньше запаса — копию не делаем вообще.
Заполненный диск останавливает запись в SQLite, то есть останавливает бота: это
хуже отсутствующего бэкапа.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .core import connect, db_path

logger = logging.getLogger(__name__)

_PREFIX = "hde_bot-"
_SUFFIX = ".db"

# Запас поверх размера базы: VACUUM INTO пишет новый файл целиком, и на пике
# на диске лежат и база, и снапшот.
_FREE_SPACE_FACTOR = 1.3


def backup_name(now: datetime) -> str:
    return f"{_PREFIX}{now:%Y-%m-%d}{_SUFFIX}"


def list_backups(dest_dir: str | Path) -> list[Path]:
    """Снапшоты от старых к новым. Имя — дата, поэтому сортировка лексическая."""
    directory = Path(dest_dir)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"{_PREFIX}*{_SUFFIX}") if p.is_file()
    )


def prune_backups(dest_dir: str | Path, keep: int) -> list[Path]:
    """Удаляет всё, кроме `keep` самых свежих. Возвращает удалённые пути."""
    backups = list_backups(dest_dir)
    if keep < 0 or len(backups) <= keep:
        return []
    doomed = backups[: len(backups) - keep] if keep else backups
    removed = []
    for path in doomed:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            logger.warning("DB backup: could not remove %s: %s", path, exc)
    return removed


async def backup_database(
    dest_dir: str | Path | None = None,
    *,
    keep: int | None = None,
    _now: datetime | None = None,
) -> Path:
    """Снимает суточный снапшот. Возвращает путь (уже существующий — как есть).

    RuntimeError — если снимать нельзя (не хватает места): молча пропустить
    бэкап значит потом узнать об этом в самый неподходящий момент, поэтому
    вызывающий (планировщик) обязан сообщить оператору.
    """
    from ..config import config

    directory = Path(dest_dir if dest_dir is not None else config.db_backup_dir)
    keep = config.db_backup_keep if keep is None else keep
    now = _now or datetime.now(timezone.utc)

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / backup_name(now)
    if target.exists():
        # Тот же день: VACUUM INTO всё равно откажется писать в существующий
        # файл, а перезаписывать нечего — снапшот за сутки уже есть.
        return target

    # Чистим ДО снятия: место освобождают старые копии, а не новая.
    prune_backups(directory, max(keep - 1, 0))

    source = Path(db_path())
    db_size = source.stat().st_size if source.exists() else 0
    free = shutil.disk_usage(directory).free
    if free < db_size * _FREE_SPACE_FACTOR:
        raise RuntimeError(
            f"недостаточно места: база {db_size / 1e6:.0f} МБ, свободно "
            f"{free / 1e6:.0f} МБ (нужен запас ×{_FREE_SPACE_FACTOR})"
        )

    async with connect() as db:
        await db.execute("VACUUM INTO ?", (str(target),))
    logger.info(
        "DB backup: %s (%.1f МБ из %.1f МБ базы)",
        target, target.stat().st_size / 1e6, db_size / 1e6,
    )
    return target

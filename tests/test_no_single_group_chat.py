"""Sentinel: Telegram calls must target the topic's own group, not the single-group setting.

Only config (defines it), operators (builds the primary operator from it) and
db/core (start-up migration backfill) may name it.
"""
from pathlib import Path

ALLOWED = {"bot/config.py", "bot/operators.py", "bot/db/core.py"}


def test_group_chat_id_only_in_allowed_files():
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "bot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "group_chat_id" in line:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "use the topic record's chat_id or operators.primary():\n" + "\n".join(offenders)

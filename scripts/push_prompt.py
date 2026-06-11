"""Залить промт-кандидат в prompt_versions. Запускается на VPS.

Usage (на VPS, из /opt/hde-bot):
    python3 scripts/push_prompt.py candidate.md            # добавить как candidate
    python3 scripts/push_prompt.py candidate.md --apply    # сразу применить

После --apply нужен рестарт бота (кеш промта в памяти):
    sudo systemctl restart hde-bot

Локальный цикл целиком:
    python scripts/eval_prompt.py --prompt candidate.md    # убедиться, что лучше
    scp candidate.md config1:/opt/hde-bot/
    ssh config1 "cd /opt/hde-bot && python3 scripts/push_prompt.py candidate.md --apply && sudo systemctl restart hde-bot"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file")
    ap.add_argument("--db", default="hde_bot.db")
    ap.add_argument("--apply", action="store_true", help="сразу сделать активной")
    args = ap.parse_args()

    text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if len(text) < 50:
        print("ERROR: промт подозрительно короткий, отмена", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    cur = conn.execute(
        "INSERT INTO prompt_versions (content, status, proposed_by) "
        "VALUES (?, 'candidate', 'manual')",
        (text,),
    )
    vid = cur.lastrowid
    if args.apply:
        conn.execute(
            "UPDATE prompt_versions SET status='rejected' "
            "WHERE status IN ('active', 'candidate') AND id != ?",
            (vid,),
        )
        conn.execute(
            "UPDATE prompt_versions SET status='active', applied_at=datetime('now') "
            "WHERE id=?",
            (vid,),
        )
    conn.commit()
    state = "применена (нужен restart бота)" if args.apply else "сохранена как candidate"
    print(f"Версия {vid} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

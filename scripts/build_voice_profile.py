"""Собирает профиль голоса оператора из стянутой с VPS базы.

Usage:
    bash scripts/pull_kb.sh
    python scripts/build_voice_profile.py
    # → bot/prompts/voice_profile.json (закоммитить и задеплоить)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.voice_profile import extract_voice_examples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/hde_bot_vps.db")
    ap.add_argument("--out", default="bot/prompts/voice_profile.json")
    ap.add_argument("--max", type=int, default=8)
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: база не найдена: {args.db}. Сначала bash scripts/pull_kb.sh", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    rows = [
        r[0]
        for r in conn.execute(
            # Только 'corrected': этот текст оператор набирал сам.
            # 'sent' может быть ИИ-ответом, отправленным как есть, — в эталон
            # голоса его брать нельзя.
            "SELECT op_answer FROM optimization_samples "
            "WHERE op_answer IS NOT NULL AND op_answer != '' "
            "AND outcome = 'corrected' "
            "ORDER BY created_at DESC"
        )
    ]
    examples = extract_voice_examples(rows, max_examples=args.max)
    if not examples:
        print("Нет подходящих op_answer — профиль не создан", file=sys.stderr)
        return 1

    Path(args.out).write_text(
        json.dumps({"examples": examples}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Сохранено {len(examples)} примеров в {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

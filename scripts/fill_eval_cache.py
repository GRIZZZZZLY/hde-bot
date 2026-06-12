"""Заливает готовые ответы (сгенерированные Claude-агентами) в data/eval_cache.db.

После заливки scripts/eval_prompt.py считает скоры офлайн, не дёргая LLM API.
Ключ кеша: sha1(prompt_hash | title | history) — title и history берутся из базы
СЫРЫМИ (None форматируется как "None"), ровно как в eval_prompt.make_cached_generate.

Usage:
    python -X utf8 scripts/fill_eval_cache.py --prompt artifacts/current_prompt.md \
        --answers artifacts/gen_current_1.json artifacts/gen_current_2.json
    # повторить для кандидата со своими файлами
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_prompt import EvalCache, _prompt_hash


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="файл с текстом промта")
    ap.add_argument("--answers", nargs="+", required=True, help="json-файлы {id: ответ}")
    ap.add_argument("--db", default="data/hde_bot_vps.db")
    ap.add_argument("--cache", default="data/eval_cache.db")
    args = ap.parse_args()

    prompt_text = Path(args.prompt).read_text(encoding="utf-8")
    ph = _prompt_hash(prompt_text)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    samples = {
        str(r["id"]): r
        for r in conn.execute("SELECT id, title, history FROM optimization_samples")
    }

    cache = EvalCache(args.cache)
    total = 0
    for f in args.answers:
        answers = json.loads(Path(f).read_text(encoding="utf-8"))
        for sid, answer in answers.items():
            s = samples.get(str(sid))
            if s is None:
                print(f"WARN: сэмпл {sid} не найден в базе", file=sys.stderr)
                continue
            key = hashlib.sha1(
                f"{ph}|{s['title']}|{s['history']}".encode("utf-8")
            ).hexdigest()
            cache.put(key, answer)
            total += 1

    print(f"Залито {total} ответов в {args.cache} (prompt_hash={ph})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

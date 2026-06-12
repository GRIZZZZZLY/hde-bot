"""Офлайн eval-харнесс: сравнение промт-кандидата с активным на реальных данных.

Usage:
    bash scripts/pull_kb.sh                                  # стянуть свежую базу
    python scripts/eval_prompt.py --prompt candidate.md      # файл с кандидатом
    python scripts/eval_prompt.py --version 12               # версия из prompt_versions
    python scripts/eval_prompt.py                            # active vs последний candidate
    python scripts/eval_prompt.py --no-judge                 # быстрый прогон без судьи

Требуется GROQ_API_KEY в окружении. Ответы кешируются в data/eval_cache.db —
прерванный прогон продолжается с того же места, повторные прогоны бесплатны.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows-консоль по умолчанию cp1251 — эмодзи в отчёте роняют print
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

from bot.optimizer import evaluator
from bot.optimizer.dataset import split_samples
from bot.optimizer.judge import holdout_score

MIN_SAMPLES_WARN = 30


def _prompt_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class EvalCache:
    """Кеш сгенерированных ответов: (prompt, history, title) → answer."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_cache (key TEXT PRIMARY KEY, answer TEXT)"
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT answer FROM eval_cache WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def put(self, key: str, answer: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO eval_cache (key, answer) VALUES (?,?)", (key, answer)
        )
        self.conn.commit()


def make_cached_generate(cache: EvalCache, prompt_text: str):
    ph = _prompt_hash(prompt_text)

    async def generate(history: str, title: str, instructions: str) -> str:
        key = hashlib.sha1(f"{ph}|{title}|{history}".encode("utf-8")).hexdigest()
        hit = cache.get(key)
        if hit is not None:
            return hit
        # Простой backoff на 429/сетевые ошибки; после 3 неудач — пробрасываем,
        # combined_score пропустит сэмпл, а кеш позволит дорешать повторным запуском.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                answer = await evaluator._generate_answer(history, title, instructions)
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(10 * (attempt + 1))
        else:
            raise last_exc  # type: ignore[misc]
        cache.put(key, answer)
        return answer

    return generate


def load_samples(db_path: str, days: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, confidence "
        "FROM optimization_samples WHERE created_at >= datetime('now', ?) ORDER BY id",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def load_prompt_from_db(db_path: str, version: int | None) -> tuple[str, str]:
    """Returns (label, content). version=None → последний candidate."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if version is not None:
        row = conn.execute(
            "SELECT id, content FROM prompt_versions WHERE id=?", (version,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, content FROM prompt_versions WHERE status='candidate' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("Кандидат не найден в prompt_versions")
    return f"v{row['id']}", row["content"]


def load_active_prompt(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT content FROM prompt_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    # Нет активной версии в БД — бот в этом случае использует встроенную
    # инструкцию (см. get_active_format_instructions), сравниваем с ней же.
    from bot.ai_summary import _FORMAT_INSTRUCTIONS

    print("В prompt_versions нет активной версии — за active берём встроенный _FORMAT_INSTRUCTIONS")
    return _FORMAT_INSTRUCTIONS


async def evaluate_prompt(
    label: str,
    prompt_text: str,
    train: list[dict],
    holdout: list[dict],
    cache: EvalCache,
    use_judge: bool,
) -> dict:
    generate = make_cached_generate(cache, prompt_text)
    train_score = await evaluator.combined_score(
        train, prompt_text, max_samples=None, _generate_fn=generate
    )
    if use_judge:
        hold = await holdout_score(holdout, prompt_text, _generate_fn=generate)
    else:
        hold = await evaluator.combined_score(
            holdout, prompt_text, max_samples=None, _generate_fn=generate
        )
    return {"label": label, "train": train_score, "holdout": hold}


async def worst_regressions(
    holdout: list[dict],
    active_text: str,
    cand_text: str,
    cache: EvalCache,
    top_n: int = 3,
) -> list[tuple[float, dict, str]]:
    gen_active = make_cached_generate(cache, active_text)
    gen_cand = make_cached_generate(cache, cand_text)
    diffs: list[tuple[float, dict, str]] = []
    for s in holdout:
        ref = s.get("op_answer") or s.get("ai_answer") or ""
        if not ref:
            continue
        a = await gen_active(s["history"], s["title"], active_text)
        c = await gen_cand(s["history"], s["title"], cand_text)
        ra = difflib.SequenceMatcher(None, a.lower(), ref.lower()).ratio()
        rc = difflib.SequenceMatcher(None, c.lower(), ref.lower()).ratio()
        diffs.append((rc - ra, s, c))
    diffs.sort(key=lambda x: x[0])
    return [d for d in diffs if d[0] < 0][:top_n]


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/hde_bot_vps.db")
    ap.add_argument("--prompt", help="файл с промтом-кандидатом")
    ap.add_argument("--version", type=int, help="id версии из prompt_versions")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--cache", default="data/eval_cache.db")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: база не найдена: {args.db}. Сначала bash scripts/pull_kb.sh", file=sys.stderr)
        return 1

    samples = load_samples(args.db, args.days)
    if len(samples) < MIN_SAMPLES_WARN:
        print(
            f"⚠️  Всего {len(samples)} сэмплов (< {MIN_SAMPLES_WARN}) — "
            "оценка ненадёжна, относись к цифрам скептически."
        )
    if not samples:
        print("ERROR: optimization_samples пуст", file=sys.stderr)
        return 1

    train, holdout = split_samples(samples)
    if not holdout or not train:
        train = holdout = samples
        print("⚠️  Сплит вырожден: train == holdout — оценка не защищена от переобучения.")
    print(f"Сэмплов: {len(samples)} (train {len(train)} / holdout {len(holdout)})")

    active_text = load_active_prompt(args.db)
    if args.prompt:
        cand_label, cand_text = Path(args.prompt).name, Path(args.prompt).read_text(encoding="utf-8")
    else:
        cand_label, cand_text = load_prompt_from_db(args.db, args.version)

    cache = EvalCache(args.cache)
    use_judge = not args.no_judge

    results = []
    for label, text in (("active", active_text), (cand_label, cand_text)):
        print(f"Оцениваю «{label}»...")
        results.append(await evaluate_prompt(label, text, train, holdout, cache, use_judge))

    print("\n=== Результаты ===")
    print(f"{'промт':<20} {'train':>8} {'holdout':>8}")
    for r in results:
        print(f"{r['label']:<20} {r['train']:>8.3f} {r['holdout']:>8.3f}")
    delta = results[1]["holdout"] - results[0]["holdout"]
    verdict = "✅ кандидат лучше" if delta > 0 else "❌ кандидат не лучше"
    print(f"\nДельта на holdout: {delta:+.3f} — {verdict}")

    regs = await worst_regressions(holdout, active_text, cand_text, cache)
    if regs:
        print("\n=== Худшие регрессии (кандидат хуже active) ===")
        for diff, s, cand_answer in regs:
            print(f"\n[{diff:+.3f}] тикет {s['ticket_id']}: {s.get('title', '')[:60]}")
            print(f"  Оператор: {(s.get('op_answer') or '')[:150]}")
            print(f"  Кандидат: {cand_answer[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))

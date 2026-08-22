"""Подбор гейта для чанков внутренней БЗ: когда статью вообще стоит показывать.

Второй замер (kb_score_measurement_v2) показал, что абсолютный косинус решить
задачу не может: настоящие top-1 лежат в 0.839-0.882, и в этот же диапазон
попадают явно неверные ("Мышку будто дабл кликает" -> "Дальнейшая работа с
клиентом", 0.846). Причина не в пороге — внутренняя БЗ просто не покрывает
железо (Атол, X-printer, PAX), она про регламенты смен и работу в админке.

Значит нужен гейт, который молчит на большинстве тикетов и срабатывает только на
покрытых. Проверяем три варианта, включая AND-гейт: чанк обязан войти и в
cosine-топ, и в BM25-топ. Вектора кешируются в .npz, чтобы перебирать гейты без
повторного прогона модели.
"""
import json
import os
import re
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.environ.get("BOT_ROOT", "d:/HDE_bot"))
from bot.knowledge.chunker import (  # noqa: E402
    TARGET_CHARS,
    chunk_markdown,
    is_navigational,
)
from bot.knowledge.indexer import clean_for_embedding  # noqa: E402

KB_DIR = os.environ.get("KB_DIR", "d:/HDE_bot/for_Maximus/01_knowledge_base")
QUERIES = os.environ.get("QUERIES", "d:/HDE_bot/data/retrieval_dump.json")
CACHE = os.environ.get("CACHE", "d:/HDE_bot/artifacts/kb_vectors.npz")
OUT = os.environ.get("OUT", "d:/HDE_bot/artifacts/kb_gate_measurement.json")

CONTROLS = [
    "Рецепт борща со свининой и свеклой",
    "Как настроить гитару шестиструнную новичку",
    "Прогноз погоды на выходные в Сочи",
    "Лучшие упражнения для спины в зале",
    "Какой фильм посмотреть вечером с семьей",
    "How to train a dog to fetch the ball",
    "История древнего Рима кратко",
    "Выбор зимней резины для кроссовера",
]


def load_chunks() -> tuple[list[str], list[str]]:
    """Вернуть (titles, chunks) по содержательным статьям дампа."""
    import hashlib

    seen: set[str] = set()
    titles: list[str] = []
    chunks: list[str] = []
    for name in sorted(os.listdir(KB_DIR)):
        if not name.endswith(".md"):
            continue
        raw = open(os.path.join(KB_DIR, name), "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        text = raw.decode("utf-8")
        if is_navigational(text):
            continue
        title = re.sub(r"^\d+_", "", name[:-3]).replace("_", " ")
        for chunk in chunk_markdown(text, TARGET_CHARS):
            titles.append(title)
            chunks.append(chunk)
    return titles, chunks


def build_fts(chunks: list[str]) -> sqlite3.Connection:
    """FTS5-индекс в памяти — тот же движок, что knowledge_fts на проде."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE fts USING fts5(content)")
    con.executemany("INSERT INTO fts(rowid, content) VALUES (?, ?)",
                    [(i + 1, c) for i, c in enumerate(chunks)])
    return con


_TOKEN_RE = re.compile(r"[\w\u0430-\u044f\u0451\u0410-\u042f\u0401-]{4,}")


def fts_top(con: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """rowid-1 лучших BM25-совпадений; запрос — OR по значимым токенам."""
    tokens = {t.lower() for t in _TOKEN_RE.findall(query)}
    if not tokens:
        return []
    expr = " OR ".join(f'"{t}"' for t in sorted(tokens))
    try:
        rows = con.execute(
            "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
            (expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] - 1 for r in rows]


def main() -> None:
    titles, chunks = load_chunks()
    tickets = json.load(open(QUERIES, encoding="utf-8"))
    queries = [f"{t['title']}\n{(t.get('history_tail') or '')[-600:]}" for t in tickets]
    print(f"chunks: {len(chunks)} | tickets: {len(tickets)}")

    if os.path.exists(CACHE):
        z = np.load(CACHE)
        if z["n_chunks"] == len(chunks):
            corpus, q_real, q_ctrl = z["corpus"], z["q_real"], z["q_ctrl"]
            print("vectors: from cache")
        else:
            os.remove(CACHE)
    if not os.path.exists(CACHE):
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("intfloat/multilingual-e5-large")

        def embed(texts, kind):
            prefix = "query: " if kind == "query" else "passage: "
            return model.encode(
                [f"{prefix}{clean_for_embedding(t)[:8000]}" for t in texts],
                batch_size=16, normalize_embeddings=True, show_progress_bar=True,
            ).astype(np.float32)

        corpus = embed(chunks, "passage")
        q_real = embed(queries, "query")
        q_ctrl = embed(CONTROLS, "query")
        np.savez(CACHE, corpus=corpus, q_real=q_real, q_ctrl=q_ctrl,
                 n_chunks=len(chunks))
        print(f"vectors -> {CACHE}")

    con = build_fts(chunks)
    real = q_real @ corpus.T
    ctrl = q_ctrl @ corpus.T

    def evaluate(name, floor, require_fts, pool=10):
        """Сколько тикетов пробивают гейт и что именно им досталось."""
        fired = []
        for i, t in enumerate(tickets):
            order = np.argsort(-real[i])[:pool]
            lexical = set(fts_top(con, queries[i], pool)) if require_fts else None
            for j in order:
                if real[i, j] < floor:
                    break
                if lexical is not None and j not in lexical:
                    continue
                fired.append({"ticket": t["title"], "article": titles[j],
                              "score": float(real[i, j]), "chunk": chunks[j][:300]})
                break
        ctrl_fired = 0
        for i in range(len(CONTROLS)):
            order = np.argsort(-ctrl[i])[:pool]
            lexical = set(fts_top(con, CONTROLS[i], pool)) if require_fts else None
            for j in order:
                if ctrl[i, j] < floor:
                    break
                if lexical is not None and j not in lexical:
                    continue
                ctrl_fired += 1
                break
        print(f"\n### {name}: сработал на {len(fired)}/{len(tickets)} тикетов, "
              f"ложных срабатываний на контроле {ctrl_fired}/{len(CONTROLS)}")
        for f in sorted(fired, key=lambda x: -x["score"]):
            print(f"  {f['score']:.3f} | {f['ticket'][:46]:<46} -> {f['article'][:40]}")
        return {"name": name, "floor": floor, "require_fts": require_fts,
                "fired": fired, "ctrl_fired": ctrl_fired}

    report = [
        evaluate("floor 0.83 (текущий порог)", 0.83, False),
        evaluate("floor 0.86", 0.86, False),
        evaluate("floor 0.83 + AND-гейт по BM25", 0.83, True),
        evaluate("floor 0.85 + AND-гейт по BM25", 0.85, True),
        evaluate("floor 0.86 + AND-гейт по BM25", 0.86, True),
    ]
    json.dump(report, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\ndetail -> {OUT}")


if __name__ == "__main__":
    main()

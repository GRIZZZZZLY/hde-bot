"""One-off: measure cosine score distribution for RAG threshold selection.

Mimics get_rag_context query construction against the VPS knowledge snapshot.
"""
import os
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.environ.get("BOT_ROOT", "d:/HDE_bot"))
from bot.knowledge.indexer import clean_for_embedding  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

DB = os.environ.get("KB_DB", "d:/HDE_bot/data/hde_bot_vps.db")

conn = sqlite3.connect(DB)
kb = conn.execute(
    "SELECT id, content, embedding FROM knowledge_items WHERE embedding IS NOT NULL"
).fetchall()
samples = conn.execute(
    "SELECT title, history FROM optimization_samples"
).fetchall()
conn.close()

matrix = np.vstack([np.frombuffer(e, dtype=np.float32) for _, _, e in kb])
norms = np.linalg.norm(matrix, axis=1)

model = SentenceTransformer("intfloat/multilingual-e5-large")


def top_scores(query: str, k: int = 3) -> list[float]:
    q = model.encode(f"query: {clean_for_embedding(query)[:8000]}", normalize_embeddings=True)
    q = q.astype(np.float32)
    scores = (matrix @ q) / np.maximum(norms * np.linalg.norm(q), 1e-12)
    return sorted(scores.tolist(), reverse=True)[:k]


real_top1, real_top3 = [], []
for title, history in samples:
    query = f"{title}\n{(history or '')[-600:]}"
    s = top_scores(query)
    real_top1.append(s[0])
    real_top3.append(s[-1])

# Control: queries unrelated to POS support — baseline e5 similarity
controls = [
    "Рецепт борща со свининой и свеклой",
    "Как настроить гитару шестиструнную новичку",
    "Прогноз погоды на выходные в Сочи",
    "Лучшие упражнения для спины в зале",
    "Какой фильм посмотреть вечером с семьей",
    "How to train a dog to fetch the ball",
    "История древнего Рима кратко",
    "Выбор зимней резины для кроссовера",
]
ctrl_top1 = [top_scores(q)[0] for q in controls]


def pct(arr, p):
    return float(np.percentile(arr, p))


print(f"REAL  top1: min={min(real_top1):.3f} p10={pct(real_top1,10):.3f} "
      f"p25={pct(real_top1,25):.3f} med={pct(real_top1,50):.3f} max={max(real_top1):.3f}")
print(f"REAL  top3: min={min(real_top3):.3f} p10={pct(real_top3,10):.3f} "
      f"p25={pct(real_top3,25):.3f} med={pct(real_top3,50):.3f} max={max(real_top3):.3f}")
print(f"CTRL  top1: min={min(ctrl_top1):.3f} med={pct(ctrl_top1,50):.3f} max={max(ctrl_top1):.3f}")
print("real top1 sorted:", " ".join(f"{x:.3f}" for x in sorted(real_top1)))
print("ctrl top1 sorted:", " ".join(f"{x:.3f}" for x in sorted(ctrl_top1)))

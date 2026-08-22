"""One-off: does the Teamly KB dump clear RAG_MIN_SCORE?

Gate for importing for_Maximus/01_knowledge_base into knowledge_items. The prod
threshold (RAG_MIN_SCORE=0.83) was tuned on ticket-shaped items; KB articles are
headings and instructions, so cosine against a client message may sit lower. If
it does, importing is pointless without a per-source threshold.

Chunks the markdown locally (throwaway — promoted to bot/knowledge/chunker.py
only if this passes), embeds with the prod model, and scores the same 30 real
tickets used by _measure_rag_scores.py.
"""
import hashlib
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.environ.get("BOT_ROOT", "d:/HDE_bot"))
from bot.knowledge.indexer import clean_for_embedding  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

KB_DIR = os.environ.get("KB_DIR", "d:/HDE_bot/for_Maximus/01_knowledge_base")
QUERIES = os.environ.get("QUERIES", "d:/HDE_bot/data/retrieval_dump.json")
OUT = os.environ.get("OUT", "d:/HDE_bot/artifacts/kb_score_measurement.json")

# 1500 = the size planned for chunker.py; 600 ≈ the size the shipped Chroma dump
# used (median 501 chars), included to check whether that chunking was any good.
SIZES = (600, 1500)
OVERLAP = 200

_HEADING_RE = re.compile(r"^#{1,6} ", re.M)


def split_blocks(text: str) -> list[str]:
    """Split markdown into heading-led blocks, preserving the heading line."""
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    if not starts or starts[0] > 0:
        starts.insert(0, 0)
    return [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)]) if text[a:b].strip()]


def hard_split(block: str, target: int, overlap: int) -> list[str]:
    """Slice an oversized block on paragraph boundaries with overlap."""
    paras = [p for p in re.split(r"\n\s*\n", block) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > target:
            out.append(cur)
            cur = (cur[-overlap:] + "\n\n" + p) if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur.strip():
        out.append(cur)
    return out


def chunk_markdown(text: str, target: int, overlap: int) -> list[str]:
    """Greedily pack heading-led blocks up to target chars."""
    chunks, cur = [], ""
    for block in split_blocks(text):
        if len(block) > target * 1.5:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(hard_split(block, target, overlap))
            continue
        if cur and len(cur) + len(block) > target:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur.strip():
        chunks.append(cur)
    return chunks


_FRONTMATTER_RE = re.compile(r"^#.*?\n- \*\*URL:\*\*.*?\n---\n", re.S)

# Навигационные статьи Teamly — повторённые меню разделов без единого
# предложения. Первый прогон показал, что они выигрывают top-1 у 8 из 30
# тикетов ("Posiflora", "Billing", "API"): плотный набор ключевых слов
# (Атол, Эвотор, кассы, AnyDesk) бьёт настоящую инструкцию по косинусу.
_MIN_PROSE_LINES = 3


def is_navigational(text: str) -> bool:
    """True для меню-статей: меньше _MIN_PROSE_LINES строк длиной 60+ символов."""
    body = _FRONTMATTER_RE.sub("", text)
    prose = sum(1 for line in body.splitlines() if len(line.strip()) >= 60)
    return prose < _MIN_PROSE_LINES


def load_unique_docs() -> tuple[list[tuple[str, str]], list[str]]:
    """Return (docs, dropped_titles) — the dump repeats 63 of 173 files."""
    seen: set[str] = set()
    docs: list[tuple[str, str]] = []
    dropped: list[str] = []
    for name in sorted(os.listdir(KB_DIR)):
        if not name.endswith(".md"):
            continue
        raw = open(os.path.join(KB_DIR, name), "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        title = re.sub(r"^\d+_", "", name[:-3]).replace("_", " ")
        text = raw.decode("utf-8")
        if is_navigational(text):
            dropped.append(title)
            continue
        docs.append((title, text))
    return docs, dropped


# Control: queries unrelated to POS support — same set as _measure_rag_scores.py
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


def pct(arr, p):
    return float(np.percentile(arr, p))


def main() -> None:
    docs, dropped = load_unique_docs()
    tickets = json.load(open(QUERIES, encoding="utf-8"))
    print(f"corpus: {len(docs)} unique articles | queries: {len(tickets)} tickets")
    print(f"dropped as navigational ({len(dropped)}): {', '.join(dropped)}")

    model = SentenceTransformer("intfloat/multilingual-e5-large")

    # Queries are identical across chunk sizes — embed once.
    def embed(texts, kind):
        prefix = "query: " if kind == "query" else "passage: "
        return model.encode(
            [f"{prefix}{clean_for_embedding(t)[:8000]}" for t in texts],
            batch_size=16, normalize_embeddings=True, show_progress_bar=True,
        ).astype(np.float32)

    q_real = embed(
        [f"{t['title']}\n{(t.get('history_tail') or '')[-600:]}" for t in tickets], "query"
    )
    q_ctrl = embed(CONTROLS, "query")

    report = {}
    for target in SIZES:
        titles, texts = [], []
        for title, text in docs:
            for chunk in chunk_markdown(text, target, OVERLAP):
                titles.append(title)
                texts.append(chunk)
        print(f"\n=== target={target} -> {len(texts)} chunks ===")
        matrix = embed(texts, "passage")

        real = q_real @ matrix.T  # both normalized → dot == cosine
        ctrl = q_ctrl @ matrix.T
        top1 = real.max(axis=1)
        top3 = np.sort(real, axis=1)[:, -3]
        best = real.argmax(axis=1)
        ctrl_top1 = ctrl.max(axis=1)

        print(f"REAL top1: min={top1.min():.3f} p10={pct(top1,10):.3f} "
              f"p25={pct(top1,25):.3f} med={pct(top1,50):.3f} max={top1.max():.3f}")
        print(f"REAL top3: min={top3.min():.3f} med={pct(top3,50):.3f} max={top3.max():.3f}")
        print(f"CTRL top1: min={ctrl_top1.min():.3f} med={pct(ctrl_top1,50):.3f} "
              f"max={ctrl_top1.max():.3f}")
        print(f"pass 0.83: {int((top1 >= 0.83).sum())}/{len(top1)} tickets")
        print("\nper-ticket top1 (score | ticket -> matched article):")
        for t, s, b in sorted(zip(tickets, top1, best), key=lambda x: -x[1]):
            print(f"  {s:.3f} | {t['title'][:52]:<52} -> {titles[b][:48]}")

        # Топ-3 попадания на тикет: сам порог пропускает 30/30, поэтому решает
        # не скор, а верна ли статья — размечать удобнее по трём кандидатам.
        top3_idx = np.argsort(real, axis=1)[:, -3:][:, ::-1]
        report[str(target)] = {
            "chunks": len(texts),
            "real_top1": top1.tolist(),
            "real_top3": top3.tolist(),
            "ctrl_top1": ctrl_top1.tolist(),
            "pass_083": int((top1 >= 0.83).sum()),
            "dropped_navigational": dropped,
            "matches": [
                {"ticket": t["title"], "score": float(s), "article": titles[b],
                 "chunk": texts[b][:400],
                 "top3": [{"article": titles[j], "score": float(real[i, j]),
                           "chunk": texts[j][:300]} for j in top3_idx[i]]}
                for i, (t, s, b) in enumerate(zip(tickets, top1, best))
            ],
        }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(report, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\ndetail -> {OUT}")


if __name__ == "__main__":
    main()

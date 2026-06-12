"""One-off: dump top-K retrieval candidates per optimization sample.

Runs the real hybrid pipeline (cosine + BM25 RRF) against the knowledge base
and writes JSON for offline relevance judging (precision@k → reranker decision).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ.get("BOT_ROOT", "d:/HDE_bot"))

OUT = os.environ.get("OUT", "/tmp/retrieval_dump.json")


async def main() -> None:
    import sqlite3
    from bot import db as _db
    from bot.knowledge.indexer import clean_for_embedding, embed_text
    from bot.knowledge.store import find_similar

    conn = sqlite3.connect(_db.DB_PATH)
    samples = conn.execute(
        "SELECT ticket_id, title, history FROM optimization_samples"
    ).fetchall()
    conn.close()

    rows = []
    for ticket_id, title, history in samples:
        query = f"{title}\n{(history or '')[-600:]}"
        emb = await embed_text(clean_for_embedding(query), task_type="query")
        if emb is None:
            continue
        similar = await find_similar(emb, limit=5, query_text=query)
        rows.append({
            "ticket_id": ticket_id,
            "title": title,
            "history_tail": (history or "")[-600:],
            "candidates": [
                {
                    "item_id": item.id,
                    "score": round(score, 4),
                    "content": item.content[:500],
                }
                for item, score in similar
            ],
        })

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"dumped {len(rows)} samples -> {OUT}")


asyncio.run(main())

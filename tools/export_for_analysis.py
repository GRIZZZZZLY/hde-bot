#!/usr/bin/env python3
"""Export knowledge base and feedback samples from local DB for offline analysis.

Usage:
    python tools/export_for_analysis.py [--db hde_bot_local.db]

Outputs to tools/analysis/:
    knowledge_items.json      — all KB entries
    feedback_samples.json     — optimization_samples (accepted + corrected)
    stats.json                — aggregate counts
    few_shot_draft.json       — top-5 accepted samples formatted for few-shot use
"""
import argparse
import json
import sqlite3
from pathlib import Path

OUT_DIR = Path(__file__).parent / "analysis"


def export(db_path: str) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # --- knowledge_items ---
    rows = con.execute(
        "SELECT id, title, content, quality, company_name, source, created_at "
        "FROM knowledge_items ORDER BY created_at DESC"
    ).fetchall()
    ki = [dict(r) for r in rows]
    (OUT_DIR / "knowledge_items.json").write_text(
        json.dumps(ki, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"knowledge_items: {len(ki)} rows")

    # --- feedback_samples (accepted + corrected only) ---
    rows = con.execute(
        "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, created_at "
        "FROM optimization_samples "
        "WHERE outcome IN ('accepted', 'corrected') "
        "ORDER BY created_at DESC"
    ).fetchall()
    fs = [dict(r) for r in rows]
    (OUT_DIR / "feedback_samples.json").write_text(
        json.dumps(fs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"feedback_samples (accepted+corrected): {len(fs)} rows")

    # --- stats ---
    total = con.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
    by_quality = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT quality, COUNT(*) FROM knowledge_items GROUP BY quality"
        ).fetchall()
    }
    by_outcome = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT outcome, COUNT(*) FROM optimization_samples GROUP BY outcome"
        ).fetchall()
    }
    stats = {"knowledge_items_total": total, "by_quality": by_quality, "by_outcome": by_outcome}
    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"stats: {stats}")

    # --- few_shot_draft: top-5 accepted, diverse by company ---
    accepted = con.execute(
        "SELECT ticket_id, title, ai_answer, op_answer, outcome, created_at "
        "FROM optimization_samples WHERE outcome = 'accepted' "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    seen_prefixes: set[str] = set()
    draft: list[dict] = []
    for row in accepted:
        title = row["title"] or ""
        prefix = title[:20]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        # Parse ai_answer into Суть/Клиенту/Памятка sections
        ai = row["ai_answer"] or ""
        suit = client = memo = ""
        for line in ai.splitlines():
            l = line.strip()
            if l.lower().startswith("суть:"):
                suit = l[5:].strip()
            elif l.lower().startswith("клиенту:"):
                client = l[8:].strip()
            elif l.lower().startswith("памятка:"):
                memo = l[8:].strip()
        if suit and client:
            draft.append({
                "ticket_id": row["ticket_id"],
                "problem": title,
                "suit": suit,
                "client": client,
                "pamyatka": memo,
                "op_answer": row["op_answer"] or "",
                "note": "REVIEW: anonymize before committing to bot/prompts/few_shot_examples.json",
            })
        if len(draft) >= 5:
            break

    (OUT_DIR / "few_shot_draft.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"few_shot_draft: {len(draft)} examples")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hde_bot_local.db")
    args = parser.parse_args()
    export(args.db)

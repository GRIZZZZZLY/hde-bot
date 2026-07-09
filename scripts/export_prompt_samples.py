"""
Export optimization_samples from hde_bot.db into a human-readable markdown file
for offline prompt analysis.

Usage on VPS:
    cd /opt/hde-bot
    python3 scripts/export_prompt_samples.py > artifacts/prompt_samples.md

Then scp the artifact back to the local machine:
    scp config1:/opt/hde-bot/artifacts/prompt_samples.md artifacts/
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hde_bot.db", help="Path to hde_bot.db")
    ap.add_argument("--days", type=int, default=30, help="Lookback window")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT created_at, ticket_id, title, history, ai_answer, op_answer,
               outcome, confidence
        FROM optimization_samples
        WHERE created_at >= datetime('now', ? )
        ORDER BY
          CASE outcome
            WHEN 'rejected'  THEN 0
            WHEN 'corrected' THEN 1
            WHEN 'sent'      THEN 2
            WHEN 'accepted'  THEN 3
            ELSE 4
          END,
          created_at DESC
        """,
        (f"-{args.days} days",),
    ).fetchall()

    outcomes = Counter(r["outcome"] for r in rows)

    out = sys.stdout
    out.write(f"# Prompt samples — last {args.days} days\n\n")
    out.write(f"Total: **{len(rows)}** samples\n\n")
    out.write("Breakdown:\n")
    for key in ("rejected", "corrected", "sent", "accepted"):
        if outcomes.get(key):
            out.write(f"- {key}: {outcomes[key]}\n")
    extras = {k: v for k, v in outcomes.items() if k not in ("rejected", "corrected", "sent", "accepted")}
    for k, v in extras.items():
        out.write(f"- {k}: {v}\n")
    out.write("\n---\n\n")

    for idx, r in enumerate(rows, start=1):
        title = (r["title"] or "").strip() or "(без заголовка)"
        conf = r["confidence"] if r["confidence"] is not None else "—"
        out.write(f"## Sample {idx} — {r['created_at']} — outcome=**{r['outcome']}** — conf={conf}\n\n")
        out.write(f"**Ticket:** `{r['ticket_id']}` — {title}\n\n")
        history = (r["history"] or "").strip()
        out.write("**История клиента:**\n\n")
        out.write("```\n")
        out.write(history if history else "(пусто)")
        out.write("\n```\n\n")
        out.write("**Ответ AI:**\n\n")
        out.write("```\n")
        out.write((r["ai_answer"] or "").strip() or "(пусто)")
        out.write("\n```\n\n")
        op_answer = (r["op_answer"] or "").strip()
        out.write("**Ответ оператора (что отправил реально):**\n\n")
        out.write("```\n")
        out.write(op_answer if op_answer else "(не отправлял / нет данных)")
        out.write("\n```\n\n")
        out.write("---\n\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Cursor-resumable backfill of dialogue_pairs from closed HDE tickets (Phase 2A).

  python scripts/backfill_dialogue_pairs.py --pages 20   # scan up to 20 pages
  python scripts/backfill_dialogue_pairs.py --status     # progress only
  python scripts/backfill_dialogue_pairs.py --reembed    # fill pending embeddings

Resumable by processed ticket-id set (page numbers shift as tickets close, so the
cursor is ids, not a page). Re-runs re-scan overlapping pages harmlessly (dedup).
Per-ticket errors are logged and skipped, never aborting a page.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _status() -> None:
    import bot.db as db
    await db.init_db()
    processed = await db.list_processed_ticket_ids()
    pending = await db.list_pending_embeddings(limit=10_000)
    print(f"processed tickets={len(processed)}, pairs={await db.count_dialogue_pairs()}, "
          f"pending embeddings={len(pending)}")
    print(f"quality: {await db.count_pairs_by_quality()}")


async def _gate(limit: int) -> None:
    import bot.db as db
    from bot.agent.pair_quality import gate_pending_pairs
    await db.init_db()
    stats = await gate_pending_pairs(limit=limit)
    print(f"gate: {stats}")
    print(f"quality: {await db.count_pairs_by_quality()}")


async def _reembed() -> None:
    import bot.db as db
    from bot.agent.dialogue_mining import reembed_pending
    await db.init_db()
    fixed = await reembed_pending()
    print(f"re-embedded {fixed} pairs")


async def _run(pages: int) -> None:
    import bot.db as db
    from bot.agent.dialogue_mining import mine_ticket_pairs, staff_id_set
    from bot.config import config
    from bot.hde_api import HDEApiClient

    await db.init_db()
    client = HDEApiClient()
    staff = staff_id_set(config.hde_owner_id, config.agent_staff_user_ids)
    staff_cache: dict = {}  # динамический резолв ролей через /users/{id} (group.type)
    known = await db.dialogue_pair_hashes()
    processed = await db.list_processed_ticket_ids()
    total_new = 0
    for page in range(1, pages + 1):
        tickets, total_pages = await client.get_closed_tickets_page(
            config.hde_owner_id, page
        )
        if not tickets:
            print(f"page {page}: пусто — конец")
            break
        page_new = 0
        for ticket in tickets:
            tid = str(ticket.get("id") or ticket.get("ticket_id") or "")
            if not tid or tid in processed:
                continue
            try:
                page_new += await mine_ticket_pairs(
                    client, ticket, staff,
                    known_hashes=known, staff_cache=staff_cache,
                )
                await db.mark_ticket_processed(tid)
                processed.add(tid)
            except Exception as exc:
                await db.log_ticket_error(tid, str(exc))
                print(f"  ticket {tid}: ошибка — пропущен ({exc})")
            await asyncio.sleep(0.5)
        total_new += page_new
        print(f"page {page}/{total_pages}: +{page_new} pairs (сессия {total_new})")
        if page >= total_pages:
            print("последняя страница — конец")
            break
    print(f"Готово. Новых пар: {total_new}. Всего: {await db.count_dialogue_pairs()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reembed", action="store_true")
    parser.add_argument("--gate", type=int, default=None, help="только LLM-фильтр качества: разметить N пар")
    args = parser.parse_args()
    if args.gate is not None:
        asyncio.run(_gate(args.gate))
    elif args.status:
        asyncio.run(_status())
    elif args.reembed:
        asyncio.run(_reembed())
    else:
        asyncio.run(_run(args.pages))


if __name__ == "__main__":
    main()

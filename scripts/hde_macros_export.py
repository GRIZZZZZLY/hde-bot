"""
Export all HelpDeskEddy global macros to data/macros/*.md for LLM editing.

Usage:
    python -m scripts.hde_macros_export

Environment variables (see .env.example):
    HDE_STAFF_BASE_URL   e.g. https://posiflora.helpdeskeddy.com
    HDE_STAFF_EMAIL
    HDE_STAFF_PASSWORD

Output:
    data/macros/{id}__{name}.md   human-editable markdown (frontmatter + body)
    data/macros/.raw/{id}.json    full server payload (backup, used nowhere yet)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Local import works both as `python -m scripts.hde_macros_export` and direct run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._hde_macro_client import (  # noqa: E402
    HdeMacroClient,
    HdeStaffError,
    get_add_post_html,
    html_to_md,
)

logger = logging.getLogger("hde_macros_export")


DATA_DIR = Path("data/macros")
RAW_DIR = DATA_DIR / ".raw"


def _safe_name(s: str, limit: int = 40) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip("._ ")
    return (s[:limit] or "macro").rstrip("._ ")


def _render_markdown(macro_id: int, name: str, body_md: str) -> str:
    name_escaped = name.replace('"', '\\"')
    frontmatter = f'---\nid: {macro_id}\nname: "{name_escaped}"\n---\n\n'
    return frontmatter + (body_md or "") + "\n"


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Export HDE global macros to markdown.")
    parser.add_argument("--only", type=int, default=None, help="Export a single macro id (for testing).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dotenv()
    base = os.getenv("HDE_STAFF_BASE_URL", "").strip()
    email = os.getenv("HDE_STAFF_EMAIL", "").strip()
    password = os.getenv("HDE_STAFF_PASSWORD", "")
    if not (base and email and password):
        print("ERROR: set HDE_STAFF_BASE_URL, HDE_STAFF_EMAIL, HDE_STAFF_PASSWORD in .env", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with HdeMacroClient(base, email, password) as cli:
            items = await cli.list_macros()
            if args.only is not None:
                items = [it for it in items if it.id == args.only]
                if not items:
                    print(f"macro id={args.only} not found in list", file=sys.stderr)
                    return 1
            logger.info("Found %d macros", len(items))

            ok = 0
            skipped: list[tuple[int, str]] = []
            for item in items:
                try:
                    payload = await cli.get_macro(item.id)
                except HdeStaffError as e:
                    logger.error("skip id=%s: %s", item.id, e)
                    skipped.append((item.id, str(e)))
                    continue

                # Save raw backup first (independent of parsing success)
                (RAW_DIR / f"{item.id}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                html = get_add_post_html(payload)
                if not html:
                    logger.warning("id=%s (%s): no add_post action, skipping .md", item.id, item.name)
                    skipped.append((item.id, "no add_post action"))
                    continue

                body_md = html_to_md(html)
                md = _render_markdown(item.id, item.name, body_md)
                fname = f"{item.id}__{_safe_name(item.name)}.md"
                (DATA_DIR / fname).write_text(md, encoding="utf-8")
                logger.info("exported id=%s → %s", item.id, fname)
                ok += 1

            print(f"\nExported {ok}/{len(items)} macros to {DATA_DIR}")
            if skipped:
                print(f"Skipped {len(skipped)}:")
                for mid, reason in skipped:
                    print(f"  id={mid}: {reason}")
            return 0

    except HdeStaffError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))

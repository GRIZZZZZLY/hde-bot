"""
Import edited markdown files from data/macros/ back into HDE global macros.

Usage:
    python -m scripts.hde_macros_import                # dry-run: show what would change
    python -m scripts.hde_macros_import --apply        # actually upload changes
    python -m scripts.hde_macros_import --only 5       # restrict to a single macro id

Safety:
    * Dry-run is the default — no writes happen without --apply.
    * Each macro is refetched just before upload; we replace ONLY the first
      add_post action's HTML body (and the name, if changed). All other
      settings (status, groups, extra actions) are preserved as-is.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._hde_macro_client import (  # noqa: E402
    HdeMacroClient,
    HdeStaffError,
    get_add_post_html,
    macro_form,
    md_to_html,
    set_add_post_html,
)

logger = logging.getLogger("hde_macros_import")


DATA_DIR = Path("data/macros")


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class ParsedMarkdown:
    id: int
    name: str
    body_md: str
    path: Path


def _parse_markdown(path: Path) -> ParsedMarkdown:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing frontmatter (--- ... ---)")
    header = m.group(1)
    body = text[m.end():].strip()

    fields: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip().strip('"').strip("'")

    if "id" not in fields:
        raise ValueError(f"{path.name}: frontmatter missing 'id'")
    try:
        macro_id = int(fields["id"])
    except ValueError as e:
        raise ValueError(f"{path.name}: id is not an integer") from e
    return ParsedMarkdown(
        id=macro_id,
        name=fields.get("name", ""),
        body_md=body,
        path=path,
    )


def _preview(s: str, n: int = 300) -> str:
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + "…"


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Import edited macros back to HDE.")
    parser.add_argument("--apply", action="store_true", help="Actually upload (default is dry-run).")
    parser.add_argument("--only", type=int, default=None, help="Limit to a single macro id.")
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

    if not DATA_DIR.exists():
        print(f"ERROR: {DATA_DIR} not found. Run hde_macros_export.py first.", file=sys.stderr)
        return 2

    # Parse all .md files (skip hidden/raw dir automatically: glob only top-level *.md)
    md_files = sorted(p for p in DATA_DIR.glob("*.md"))
    parsed: list[ParsedMarkdown] = []
    for p in md_files:
        try:
            pm = _parse_markdown(p)
        except ValueError as e:
            print(f"skip {p.name}: {e}", file=sys.stderr)
            continue
        if args.only is not None and pm.id != args.only:
            continue
        parsed.append(pm)

    if not parsed:
        print("Nothing to import.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Loaded {len(parsed)} markdown file(s). Mode: {mode}\n")

    updated = unchanged = failed = 0
    try:
        async with HdeMacroClient(base, email, password) as cli:
            for pm in parsed:
                try:
                    payload = await cli.get_macro(pm.id)
                except HdeStaffError as e:
                    print(f"  [FAIL] id={pm.id}: {e}")
                    failed += 1
                    continue

                form = macro_form(payload)
                old_html = get_add_post_html(payload)
                new_html = md_to_html(pm.body_md)
                old_name = str(form.get("name", ""))
                new_name = pm.name or old_name

                html_changed = old_html.strip() != new_html.strip()
                name_changed = new_name != old_name and pm.name

                if not html_changed and not name_changed:
                    print(f"  [SKIP] id={pm.id} '{old_name}' — no changes")
                    unchanged += 1
                    continue

                print(f"  [CHG ] id={pm.id} '{old_name}'")
                if name_changed:
                    print(f"         name: {old_name!r} → {new_name!r}")
                if html_changed:
                    print(f"         old: {_preview(old_html)}")
                    print(f"         new: {_preview(new_html)}")

                if not args.apply:
                    continue

                set_add_post_html(payload, new_html)
                if name_changed:
                    form["name"] = new_name
                try:
                    await cli.update_macro(pm.id, payload)
                except HdeStaffError as e:
                    print(f"  [FAIL] id={pm.id}: update failed: {e}")
                    failed += 1
                    continue
                print(f"  [OK  ] id={pm.id} uploaded")
                updated += 1

    except HdeStaffError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(
        f"\nDone. updated={updated} unchanged={unchanged} failed={failed} "
        f"({'DRY-RUN — no writes made' if not args.apply else 'APPLIED'})"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))

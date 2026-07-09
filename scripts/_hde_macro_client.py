"""
Minimal async client for HelpDeskEddy staff web UI — global macros only.

Used by scripts/hde_macros_export.py and scripts/hde_macros_import.py.
Not wired into the main bot.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import markdown as _md
from markdownify import markdownify as _md_from_html

logger = logging.getLogger(__name__)


CSRF_META_RE = re.compile(
    r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


class HdeStaffError(RuntimeError):
    pass


@dataclass
class MacroListItem:
    id: int
    name: str
    raw: dict[str, Any]


class HdeMacroClient:
    """Thin async wrapper around the staff web UI JSON endpoints."""

    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self._session: aiohttp.ClientSession | None = None
        self._csrf: str | None = None

    async def __aenter__(self) -> "HdeMacroClient":
        jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(cookie_jar=jar)
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------ auth

    async def _extract_csrf(self, url: str) -> str | None:
        """Look for CSRF token in HTML meta, response headers, or cookies."""
        assert self._session is not None
        async with self._session.get(url) as r:
            html = await r.text(errors="ignore")
            # 1) meta tag
            m = CSRF_META_RE.search(html)
            if m:
                return m.group(1)
            # 2) response header
            for hname in ("X-CSRF-TOKEN", "X-CSRF-Token", "X-Xsrf-Token"):
                if hname in r.headers:
                    return r.headers[hname]
        # 3) cookie
        for c in self._session.cookie_jar:
            if c.key.lower() in ("xsrf-token", "csrf-token", "_csrf"):
                return c.value
        return None

    async def login(self) -> None:
        assert self._session is not None
        csrf = await self._extract_csrf(f"{self.base_url}/ru/login")
        if not csrf:
            raise HdeStaffError(
                "CSRF token not found on /ru/login — open DevTools, reload the page "
                "and tell me where the SPA keeps the token (meta tag / cookie name)."
            )
        headers = self._headers(csrf, referer=f"{self.base_url}/ru/login")
        data = [("login", self.email), ("password", self.password), ("rememberMe", "0")]
        async with self._session.post(
            f"{self.base_url}/ru/login", data=data, headers=headers
        ) as r:
            body = await r.text(errors="ignore")
            if r.status != 200:
                raise HdeStaffError(f"Login failed: HTTP {r.status}: {body[:300]}")
            logger.debug("Login response: %s", body[:200])

        # After login the SPA may rotate CSRF — refresh from a staff page.
        fresh = await self._extract_csrf(
            f"{self.base_url}/ru/staff/global_macro/?macroType=detail"
        )
        self._csrf = fresh or csrf
        logger.info("Logged in as %s", self.email)

    def _headers(self, csrf: str | None = None, *, referer: str | None = None) -> dict[str, str]:
        h = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
        }
        if csrf or self._csrf:
            h["X-CSRF-Token"] = csrf or self._csrf  # type: ignore[assignment]
        if referer:
            h["Referer"] = referer
        return h

    # --------------------------------------------------------------- macros

    async def list_macros(self) -> list[MacroListItem]:
        assert self._session is not None
        url = f"{self.base_url}/ru/staff/global_macro/list"
        referer = f"{self.base_url}/ru/staff/global_macro/?macroType=detail"
        async with self._session.get(url, headers=self._headers(referer=referer)) as r:
            if r.status != 200:
                raise HdeStaffError(f"list: HTTP {r.status}: {await r.text()}")
            js = await r.json(content_type=None)

        items = _find_macro_list(js)
        if not items:
            raise HdeStaffError(
                f"Could not locate macro array in /list response. Keys at top: "
                f"{list(js.keys()) if isinstance(js, dict) else type(js).__name__}"
            )
        out: list[MacroListItem] = []
        for it in items:
            mid = it.get("id") or it.get("macroId") or it.get("macro_id")
            nm = it.get("name") or it.get("title") or f"macro_{mid}"
            if mid is None:
                continue
            out.append(MacroListItem(id=int(mid), name=str(nm), raw=it))
        return out

    async def get_macro(self, macro_id: int) -> dict[str, Any]:
        assert self._session is not None
        url = f"{self.base_url}/ru/staff/global_macro/form/action/data/id/{macro_id}"
        referer = f"{self.base_url}/ru/staff/global_macro/?macroType=detail"
        async with self._session.get(url, headers=self._headers(referer=referer)) as r:
            if r.status != 200:
                raise HdeStaffError(
                    f"get_macro({macro_id}): HTTP {r.status}: {await r.text()}"
                )
            return await r.json(content_type=None)

    async def update_macro(self, macro_id: int, payload: dict[str, Any]) -> None:
        assert self._session is not None
        url = f"{self.base_url}/ru/staff/global_macro/form/action/update/id/{macro_id}"
        referer = f"{self.base_url}/ru/staff/global_macro/?macroType=detail"
        form = macro_form(payload)
        pruned = {k: v for k, v in form.items() if k in _UPDATE_WHITELIST}
        pairs = encode_nested_form(pruned)
        headers = self._headers(referer=referer)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        async with self._session.put(url, data=pairs, headers=headers) as r:
            body = await r.text(errors="ignore")
            if r.status != 200:
                raise HdeStaffError(
                    f"update_macro({macro_id}): HTTP {r.status}: {body[:500]}"
                )
            logger.debug("update_macro(%s) → %s", macro_id, body[:200])


# ---------------------------------------------------------------- helpers


def _find_macro_list(payload: Any) -> list[dict[str, Any]]:
    """Find an array of macro dicts inside arbitrary JSON."""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and ("id" in payload[0] or "name" in payload[0]):
            return payload
        return []
    if isinstance(payload, dict):
        # Common HDE wrappers: {"data": [...]}, {"items": [...]}, {"list": [...]}
        for key in ("data", "items", "list", "results", "macros", "rows"):
            if key in payload:
                found = _find_macro_list(payload[key])
                if found:
                    return found
        # Fallback: scan values
        for v in payload.values():
            found = _find_macro_list(v)
            if found:
                return found
    return []


def encode_nested_form(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten {a: {b: {c: 1}}, list: [1,2]} into PHP-style form pairs."""
    out: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                key = f"{prefix}[{k}]" if prefix else str(k)
                walk(key, v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)
        else:
            if value is None:
                out.append((prefix, ""))
            elif isinstance(value, bool):
                out.append((prefix, "1" if value else "0"))
            else:
                out.append((prefix, str(value)))

    walk("", data)
    return out


# ---------------------------------------------------------- add_post helpers


# Fields sent back in PUT /update. HDE returns GET data wrapped as
# {"form": {...}, "groups": {...(ui catalog)...}}; we only POST the inner
# "form" subset. Extend this set if the API silently drops a setting.
_UPDATE_WHITELIST = {
    "name",
    "content",
    "macroType",
    "actions",
    "groups",
    "description",
    "departments",
}


def macro_form(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the editable macro dict (unwraps {"form": {...}} from GET)."""
    if isinstance(payload, dict) and isinstance(payload.get("form"), dict):
        return payload["form"]
    return payload


def find_add_post_index(payload: dict[str, Any]) -> int | None:
    """Return the index of the first 'add_post' action, or None."""
    form = macro_form(payload)
    actions = form.get("actions") or []
    for i, a in enumerate(actions):
        if isinstance(a, dict) and a.get("type") == "add_post":
            return i
    return None


def get_add_post_html(payload: dict[str, Any]) -> str:
    form = macro_form(payload)
    i = find_add_post_index(form)
    if i is None:
        return ""
    value = form["actions"][i].get("value")
    if isinstance(value, dict):
        return str(value.get("content") or "")
    return str(value or "")


def set_add_post_html(payload: dict[str, Any], new_html: str) -> None:
    """Mutate payload in place: replace content of the first add_post action."""
    form = macro_form(payload)
    i = find_add_post_index(form)
    if i is None:
        raise HdeStaffError("macro has no add_post action — nothing to update")
    action = form["actions"][i]
    value = action.get("value")
    if isinstance(value, dict):
        value["content"] = new_html
    else:
        # Defensive: create the expected shape if the server returned a scalar
        action["value"] = {"content": new_html, "send_message": 0}


# ---------------------------------------------------------- HTML <-> Markdown


_LIST_ITEM_RE = re.compile(r"^(\s*)(?:\d+\.|[-*+])\s+", re.MULTILINE)


def _normalize_md_lists(md: str) -> str:
    """Make Markdown lists render as one list, not a broken chain of paragraphs.

    HDE macro bodies are often plain paragraphs with manual "1.", "2." numbering
    glued by <br>, not real <ol>/<li>. After HTML→MD conversion we get text with:
      - hard-break (trailing "  ") right before "1. item";
      - blank lines between "1." and "2." which turn into TWO lists in MD.
    Both break the renderer. This normalizer fixes that.
    """
    if not md:
        return md
    # 1) strip trailing hard-break whitespace on the line right before a list item
    md = re.sub(r"[ \t]+\n(?=[ \t]*(?:\d+\.|[-*+])[ \t])", "\n", md)
    # 2) ensure a blank line before a list item that follows a non-blank, non-list line
    lines = md.split("\n")
    out: list[str] = []
    for ln in lines:
        if _LIST_ITEM_RE.match(ln) and out and out[-1].strip() and not _LIST_ITEM_RE.match(out[-1]):
            out.append("")
        out.append(ln)
    md = "\n".join(out)
    # 3) collapse blank lines between consecutive list items
    prev = None
    while prev != md:
        prev = md
        md = re.sub(
            r"((?:^|\n)[ \t]*(?:\d+\.|[-*+])[ \t][^\n]*)\n[ \t]*\n+(?=[ \t]*(?:\d+\.|[-*+])[ \t])",
            r"\1\n",
            md,
        )
    return md


def html_to_md(html: str) -> str:
    if not html:
        return ""
    raw = _md_from_html(
        html,
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
    ).strip()
    return _normalize_md_lists(raw)


def md_to_html(md: str) -> str:
    if not md:
        return ""
    return _md.markdown(_normalize_md_lists(md), extensions=["extra", "sane_lists"]).strip()

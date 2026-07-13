from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

import aiohttp

from .config import config

logger = logging.getLogger(__name__)


# Process-wide connection pool. Short-lived ClientSessions borrow keep-alive
# connections from it (connector_owner=False), so the TCP+TLS handshake happens
# once per pooled connection instead of once per request — the win for bulk
# loops (import, refresh, pagination) on a 24/7 bot with a single event loop.
_shared_connector: aiohttp.TCPConnector | None = None
_connector_loop: asyncio.AbstractEventLoop | None = None


def _get_connector() -> aiohttp.TCPConnector:
    """Return the shared connector, (re)created per running event loop.

    Keyed by the running loop so tests — which use a fresh loop per test —
    never reuse a connector bound to a closed loop. In production the loop is
    stable, so the connector is created once and lives for the process.
    """
    global _shared_connector, _connector_loop
    loop = asyncio.get_running_loop()
    if _shared_connector is None or _shared_connector.closed or _connector_loop is not loop:
        _shared_connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=30, ttl_dns_cache=300)
        _connector_loop = loop
    return _shared_connector


def shared_session(**kwargs) -> aiohttp.ClientSession:
    """ClientSession borrowing the process-wide connector: keep-alive connections
    are reused across calls, so no TCP+TLS handshake per request."""
    return aiohttp.ClientSession(connector=_get_connector(), connector_owner=False, **kwargs)


async def close_shared_connector() -> None:
    """Close the shared connection pool. Call on bot shutdown."""
    global _shared_connector, _connector_loop
    if _shared_connector is not None and not _shared_connector.closed:
        await _shared_connector.close()
    _shared_connector = None
    _connector_loop = None


# Hard ceiling per request: without it aiohttp waits up to 5 minutes on a
# hung server, stalling webhook handlers for the whole duration.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


class _RateLimiter:
    """Process-wide client-side throttle for HDE API calls.

    HDE bans the SHARED account for 20 min on >300 req/min, taking down other
    services on the same account (incident 2026-07-13). We space our own
    requests to config.hde_api_max_rpm — kept well below 300 to leave headroom
    for those other services. Strict min-interval spacing (no bursts): a single
    lock serialises acquirers so N concurrent callers still can't burst.
    """

    def __init__(self, max_per_min: int, *, _time_fn=None, _sleep_fn=None) -> None:
        self.min_interval = 60.0 / max_per_min if max_per_min and max_per_min > 0 else 0.0
        self._time_fn = _time_fn
        self._sleep_fn = _sleep_fn
        self._lock: asyncio.Lock | None = None
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        time_fn = self._time_fn or asyncio.get_running_loop().time
        sleep_fn = self._sleep_fn or asyncio.sleep
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time_fn()
            wait = self._next_allowed - now
            if wait > 0:
                await sleep_fn(wait)
                now = time_fn()
            self._next_allowed = max(now, self._next_allowed) + self.min_interval


# Keyed by running loop (tests use a fresh loop each); prod loop is stable so a
# single limiter lives for the process and all HDEApiClient instances share it.
_rate_limiter: _RateLimiter | None = None
_rate_limiter_loop: asyncio.AbstractEventLoop | None = None


def _get_rate_limiter() -> _RateLimiter:
    global _rate_limiter, _rate_limiter_loop
    loop = asyncio.get_running_loop()
    if _rate_limiter is None or _rate_limiter_loop is not loop:
        _rate_limiter = _RateLimiter(config.hde_api_max_rpm)
        _rate_limiter_loop = loop
    return _rate_limiter


class HDEApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class HDEAttachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class HDEPost:
    post_id: int
    user_id: int
    text: str            # raw HTML from HDE
    date_created: str    # "HH:MM:SS DD.MM.YYYY"
    is_comment: bool = False  # True = internal comment, False = public post
    user_name: str = ""  # display name of the post author (if available from API)
    files: list = None   # [{"name": ..., "url": ..., "data_type": ...}]

    def __post_init__(self) -> None:
        if self.files is None:
            self.files = []


@dataclass
class HDETicketInfo:
    client_id: int
    client_name: str     # user_name + user_lastname
    owner_id: int
    owner_name: str      # owner_name + owner_lastname


@dataclass
class HDEApiResult:
    status: int
    data: Any


@dataclass
class HDETicket:
    ticket_id: str        # numeric string id
    unique_id: str        # e.g. "ABC-123"
    title: str
    company_name: str     # client name (user_name + user_lastname)
    owner_id: str
    sla_date: Optional[str]   # "17.01.2017 16:00" format or None
    hde_link: str         # staff link
    link_staff: str       # same as hde_link


class HDEApiClient:
    def __init__(self) -> None:
        if not config.has_hde_api_credentials():
            raise HDEApiError("HDE API не настроен")
        self.base_url = config.hde_api_base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(config.hde_api_email, config.hde_api_key)

    def _make_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            auth=self.auth,
            connector=_get_connector(),
            connector_owner=False,
            timeout=_REQUEST_TIMEOUT,
        )

    # GETs are idempotent, so transient failures are retried with backoff.
    # Writes are never retried.
    _GET_ATTEMPTS = 3
    _GET_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _GET_BACKOFF_BASE = 0.5  # seconds; zeroed in tests so retries don't sleep

    async def _get(self, url: str, params: dict[str, str] | None = None) -> tuple[int, Any]:
        """GET returning (status, parsed body), retrying transient failures.

        Retries on 5xx/429 responses, timeouts, and connection errors (incl.
        stale keep-alive connections from the shared pool) up to _GET_ATTEMPTS
        times with exponential backoff. The last 5xx/429 response is returned
        as-is so callers keep their existing status handling.
        """
        for attempt in range(1, self._GET_ATTEMPTS + 1):
            try:
                await _get_rate_limiter().acquire()
                async with self._make_session() as session:
                    async with session.get(url, params=params) as response:
                        if (
                            response.status not in self._GET_RETRY_STATUSES
                            or attempt == self._GET_ATTEMPTS
                        ):
                            return response.status, await self._read_response(response)
                        logger.warning(
                            "HDE GET %s: status %d, retry %d/%d",
                            url, response.status, attempt, self._GET_ATTEMPTS,
                        )
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if attempt == self._GET_ATTEMPTS:
                    raise
                logger.warning(
                    "HDE GET %s: %s, retry %d/%d",
                    url, exc.__class__.__name__, attempt, self._GET_ATTEMPTS,
                )
            await asyncio.sleep(self._GET_BACKOFF_BASE * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    async def add_comment(
        self,
        ticket_id: str,
        text: str = "",
        attachments: Sequence[HDEAttachment] = (),
    ) -> HDEApiResult:
        return await self._post(
            f"/tickets/{ticket_id}/comments/",
            text=text,
            attachments=attachments,
        )

    async def add_post(
        self,
        ticket_id: str,
        text: str = "",
        attachments: Sequence[HDEAttachment] = (),
    ) -> HDEApiResult:
        return await self._post(
            f"/tickets/{ticket_id}/posts/",
            text=text,
            attachments=attachments,
        )

    async def update_post(self, ticket_id: str, post_id: int, text: str) -> HDEApiResult:
        return await self._put(f"/tickets/{ticket_id}/posts/{post_id}/", text=text)

    async def delete_post(self, ticket_id: str, post_id: int) -> HDEApiResult:
        return await self._delete(f"/tickets/{ticket_id}/posts/{post_id}/")

    async def update_comment(self, ticket_id: str, comment_id: int, text: str) -> HDEApiResult:
        return await self._put(f"/tickets/{ticket_id}/comments/{comment_id}/", text=text)

    async def delete_comment(self, ticket_id: str, comment_id: int) -> HDEApiResult:
        return await self._delete(f"/tickets/{ticket_id}/comments/{comment_id}/")

    async def get_user_organization(self, user_id: str) -> tuple[str, str]:
        """Return (org_id, org_name) for a HDE user. Returns ('', '') if no org."""
        status, data = await self._get(f"{self.base_url}/users/{user_id}/")
        if status >= 400:
            return ("", "")
        raw = data.get("data", data) if isinstance(data, dict) else {}
        org = raw.get("organization", "")
        if isinstance(org, dict):
            return (str(org.get("id", "") or ""), str(org.get("name", "") or ""))
        return ("", "")

    async def get_ticket_info(self, ticket_id: str) -> HDETicketInfo:
        """Return client and owner identities for a ticket."""
        status, data = await self._get(f"{self.base_url}/tickets/{ticket_id}/")
        if status >= 400:
            raise HDEApiError(self._extract_error_message(data) or f"HDE API error {status}")
        raw = data.get("data", data) if isinstance(data, dict) else {}
        client_name = f"{raw.get('user_name', '')} {raw.get('user_lastname', '')}".strip() or "Клиент"
        owner_name = f"{raw.get('owner_name', '')} {raw.get('owner_lastname', '')}".strip() or "Сотрудник"
        return HDETicketInfo(
            client_id=int(raw.get("user_id", 0)),
            client_name=client_name,
            owner_id=int(raw.get("owner_id", 0)),
            owner_name=owner_name,
        )

    async def get_ticket_field_value(self, ticket_id: str, field_id: int) -> int | None:
        """Return the current select option id for a custom field.

        0 means the field is empty. None means an error occurred — caller
        must NOT assume the field is empty (fail-safe against clobbering).
        """
        url = f"{self.base_url}/tickets/{ticket_id}/"
        try:
            status, data = await self._get(url)
            if status >= 400:
                return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            for cf in raw.get("custom_fields") or []:
                if cf.get("id") != field_id:
                    continue
                fv = cf.get("field_value")
                if isinstance(fv, dict):
                    return int(fv.get("id") or 0)
                return 0
            return 0
        except Exception:
            return None

    async def get_ticket_priority_type(self, ticket_id: str) -> tuple[str, str] | None:
        """Current (priority_id, type_id) as strings, or None on any error.

        type_id may legitimately be "0" («Вопрос»)."""
        try:
            status, data = await self._get(f"{self.base_url}/tickets/{ticket_id}/")
            if status >= 400:
                return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            prio = raw.get("priority_id")
            typ = raw.get("type_id")
            return (
                str(prio) if prio is not None else "",
                str(typ) if typ is not None else "",
            )
        except Exception:
            return None

    async def get_ticket_open_status(self, ticket_id: str) -> tuple[bool, str] | None:
        """Return (is_deletable, link_staff) or None on any error (fail-safe).

        is_deletable=True means status in {resolved, closed} → safe to delete topic.
        Returns None on network/API error → caller must skip deletion.
        """
        url = f"{self.base_url}/tickets/{ticket_id}/"
        try:
            status_code, data = await self._get(url)
            if status_code >= 400:
                return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            # HDE returns the status as `status_id` (e.g. open / 6 / v-processe /
            # closed). The only terminal status is "closed" ("Выполнено").
            status = str(raw.get("status_id") or raw.get("status") or "")
            link = raw.get("link_staff", "")
            is_deletable = status == "closed"
            return (is_deletable, link)
        except Exception:
            return None

    async def get_client_tickets(self, client_id: int, limit: int = 10) -> list[dict]:
        """Return up to `limit` tickets for the given client (requester), newest first.

        Returns raw ticket dicts with keys: id, subject, status, date_created.
        """
        url = f"{self.base_url}/tickets/"
        params = {
            "user_list": str(client_id),
            "order_by": "id",
            "order_dir": "desc",
            "page": "1",
        }
        status, data = await self._get(url, params)
        if status >= 400:
            raise HDEApiError(
                self._extract_error_message(data) or f"HDE API error {status}"
            )
        items = data.get("data", []) if isinstance(data, dict) else []
        return [
            {
                "id": item.get("id"),
                "subject": item.get("subject") or item.get("name") or "",
                "status": item.get("status", ""),
                "date_created": item.get("date_created", ""),
            }
            for item in items
            if isinstance(item, dict)
        ][:limit]

    async def get_user_group_type(self, user_id: str | int) -> str | None:
        """Тип группы пользователя HDE: 'staff' для сотрудников, 'client' для
        клиентов, None при 404/ошибке. Авторитетный способ отличить оператора
        от клиента (в постах поле роли отсутствует)."""
        url = f"{self.base_url}/users/{user_id}/"
        try:
            status, data = await self._get(url, {})
        except Exception:
            return None
        if status >= 400 or not isinstance(data, dict):
            return None
        group = (data.get("data") or {}).get("group") or {}
        gtype = group.get("type")
        return str(gtype) if gtype else None

    async def get_ticket_posts(
        self, ticket_id: str, limit: int = 20, page: int | None = None
    ) -> list[HDEPost]:
        """Return up to *limit* posts (newest first from API, returned oldest-first).

        *page* is threaded into the query only when set — omitting it preserves the
        legacy single-page behaviour and existing callers/tests.
        """
        url = f"{self.base_url}/tickets/{ticket_id}/posts/"
        params = {"limit": str(limit)}
        if page is not None:
            params["page"] = str(page)
        status, data = await self._get(url, params)
        if status >= 400:
            raise HDEApiError(self._extract_error_message(data) or f"HDE API error {status}")
        items = data.get("data", []) if isinstance(data, dict) else []
        posts = [
            HDEPost(
                post_id=int(item.get("id", 0)),
                user_id=int(item.get("user_id", 0)),
                text=item.get("text", ""),
                date_created=item.get("date_created", ""),
                user_name=f"{item.get('user_name', '')} {item.get('user_lastname', '')}".strip(),
                files=item.get("files") or [],
            )
            for item in items
            if isinstance(item, dict)
        ]
        posts.reverse()  # oldest first for display
        return posts

    async def get_all_ticket_posts(
        self, ticket_id, *, page_size: int = 20, max_pages: int = 25
    ) -> list["HDEPost"]:
        """Полная история постов тикета через пагинацию (не обрезается limit=20).

        Пагинация /posts/ предполагается по аналогии с tickets-эндпоинтами (page
        поддерживается там). Порядок между страницами не гарантирован — вызывающий
        код (split_ticket_into_pairs) явно пересортирует посты. Если live-проверка
        покажет, что /posts/ игнорирует page, переключиться на один вызов
        get_ticket_posts(limit=200) — см. docs/superpowers/notes/hde-post-author-type.md.
        """
        collected: list = []
        page = 1
        while page <= max_pages:
            batch = await self.get_ticket_posts(ticket_id, limit=page_size, page=page)
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return collected

    async def get_ticket_comments(self, ticket_id: str, limit: int = 20) -> list[HDEPost]:
        """Return up to *limit* internal comments (oldest-first)."""
        url = f"{self.base_url}/tickets/{ticket_id}/comments/"
        status, data = await self._get(url, {"limit": str(limit)})
        if status >= 400:
            raise HDEApiError(self._extract_error_message(data) or f"HDE API error {status}")
        items = data.get("data", []) if isinstance(data, dict) else []
        comments = [
            HDEPost(
                post_id=int(item.get("id", 0)),
                user_id=int(item.get("user_id", 0)),
                text=item.get("text", ""),
                date_created=item.get("date_created", ""),
                is_comment=True,
                user_name=f"{item.get('user_name', '')} {item.get('user_lastname', '')}".strip(),
                files=item.get("files") or [],
            )
            for item in items
            if isinstance(item, dict)
        ]
        comments.reverse()  # oldest first for display
        return comments

    async def get_my_open_tickets(self) -> list[HDETicket]:
        """Return all open/in-progress tickets assigned to me, paginated.

        Page 1 reveals total_pages; remaining pages are fetched concurrently.
        """
        url = f"{self.base_url}/tickets/"
        base_params = {
            "owner_list": config.hde_owner_id,
            "status_list": "open,process",
        }

        status, data = await self._get(url, {**base_params, "page": "1"})
        if status >= 400:
            raise HDEApiError(self._extract_error_message(data) or f"HDE API error {status}")
        if not isinstance(data, dict):
            return []

        meta = data.get("meta", {})
        total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
        pages: list[dict] = [data]
        if total_pages > 1:
            results = await asyncio.gather(
                *(self._get(url, {**base_params, "page": str(p)}) for p in range(2, total_pages + 1))
            )
            for page_status, page_data in results:
                if page_status >= 400:
                    raise HDEApiError(self._extract_error_message(page_data) or f"HDE API error {page_status}")
                if isinstance(page_data, dict):
                    pages.append(page_data)

        all_tickets: list[HDETicket] = []
        for page_data in pages:
            tickets_data = page_data.get("data", {})
            if not isinstance(tickets_data, dict):
                continue
            for ticket_raw in tickets_data.values():
                if not isinstance(ticket_raw, dict):
                    continue
                ticket_id = str(ticket_raw.get("id", ""))
                unique_id = ticket_raw.get("unique_id", ticket_id)
                title = ticket_raw.get("title", "")
                user_name = ticket_raw.get("user_name", "")
                user_lastname = ticket_raw.get("user_lastname", "")
                company_name = f"{user_name} {user_lastname}".strip() or "—"
                sla_date = ticket_raw.get("sla_date") or None
                link_staff = f"{self.base_url.replace('/api/v2', '')}/tickets/{ticket_id}"
                all_tickets.append(HDETicket(
                    ticket_id=ticket_id,
                    unique_id=unique_id,
                    title=title,
                    company_name=company_name,
                    owner_id=str(ticket_raw.get("owner_id", "")),
                    sla_date=sla_date,
                    hde_link=link_staff,
                    link_staff=link_staff,
                ))

        return all_tickets

    async def get_unassigned_tickets(self, department_name: str = "") -> list[HDETicket]:
        """Return open/process tickets without an owner, optionally filtered by department name.

        Filters client-side by checking owner_id and (case-insensitive) department name —
        the HDE API's `owner_list=0` filter is not consistently honoured across instances.
        """
        target_dept = department_name.strip().lower()
        url = f"{self.base_url}/tickets/"
        base_params = {
            "owner_list": "0",
            "status_list": "open,process",
        }

        status, data = await self._get(url, {**base_params, "page": "1"})
        if status >= 400:
            raise HDEApiError(self._extract_error_message(data) or f"HDE API error {status}")
        if not isinstance(data, dict):
            return []

        meta = data.get("meta", {})
        total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
        pagination = data.get("pagination", {})
        if isinstance(pagination, dict):
            total_pages = max(total_pages, pagination.get("total_pages", 1))
        total_pages = min(total_pages, 30)  # safety cap — should never realistically fire

        pages: list[dict] = [data]
        if total_pages > 1:
            results = await asyncio.gather(
                *(self._get(url, {**base_params, "page": str(p)}) for p in range(2, total_pages + 1))
            )
            for page_status, page_data in results:
                if page_status >= 400:
                    raise HDEApiError(self._extract_error_message(page_data) or f"HDE API error {page_status}")
                if isinstance(page_data, dict):
                    pages.append(page_data)

        all_tickets: list[HDETicket] = []
        for page_data in pages:
            tickets_data = page_data.get("data", {})
            if isinstance(tickets_data, dict):
                items = list(tickets_data.values())
            elif isinstance(tickets_data, list):
                items = tickets_data
            else:
                continue

            for ticket_raw in items:
                if not isinstance(ticket_raw, dict):
                    continue
                # Defensive owner check (the API filter may not be reliable)
                owner_id_raw = ticket_raw.get("owner_id")
                owner_name_raw = (ticket_raw.get("owner_name") or "").strip().lower()
                has_owner_id = owner_id_raw not in (None, "", 0, "0")
                has_real_owner_name = bool(owner_name_raw) and "неприсвоен" not in owner_name_raw and "unassigned" not in owner_name_raw
                if has_owner_id and has_real_owner_name:
                    continue

                if target_dept:
                    dept = (
                        ticket_raw.get("department_name")
                        or ticket_raw.get("department")
                        or ""
                    )
                    if str(dept).strip().lower() != target_dept:
                        continue

                ticket_id = str(ticket_raw.get("id", ""))
                if not ticket_id:
                    continue
                unique_id = ticket_raw.get("unique_id") or ticket_id
                title = ticket_raw.get("title", "") or ""
                user_name = ticket_raw.get("user_name", "")
                user_lastname = ticket_raw.get("user_lastname", "")
                company_name = f"{user_name} {user_lastname}".strip() or "—"
                sla_date = ticket_raw.get("sla_date") or None
                link_staff = f"{self.base_url.replace('/api/v2', '')}/tickets/{ticket_id}"
                all_tickets.append(HDETicket(
                    ticket_id=ticket_id,
                    unique_id=unique_id,
                    title=title,
                    company_name=company_name,
                    owner_id=str(owner_id_raw or ""),
                    sla_date=sla_date,
                    hde_link=link_staff,
                    link_staff=link_staff,
                ))

        return all_tickets

    async def get_closed_tickets_page(
        self,
        owner_id: str,
        page: int = 1,
    ) -> tuple[list[dict], int]:
        """Fetch one page of closed tickets for owner_id.

        Returns (tickets_on_page, total_pages).
        Uses HDE's default page size (30) — per_page param is ignored by the API.
        """
        url = f"{self.base_url}/tickets/"
        params = {
            "owner_list": owner_id,
            "status_list": "closed",
            "page": str(page),
        }
        status, data = await self._get(url, params)
        if status >= 400:
            message = (
                self._extract_error_message(data)
                or f"HDE API error {status}"
            )
            raise HDEApiError(message)

        if not isinstance(data, dict):
            return [], 1

        tickets_data = data.get("data", {})
        if not isinstance(tickets_data, dict):
            return [], 1

        tickets = [t for t in tickets_data.values() if isinstance(t, dict)]

        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", 1) if isinstance(pagination, dict) else 1
        logger.info(
            "get_closed_tickets_page owner=%s page=%d total_pages=%d count=%d",
            owner_id, page, total_pages, len(tickets),
        )
        return tickets, total_pages

    async def get_closed_tickets(
        self,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch up to `limit` closed tickets for the given owner_id.

        NOTE: `limit` counts total fetched tickets, not unique new ones.
        For import workflows that need N *new* tickets, use get_closed_tickets_page
        directly and paginate until the desired number of new items are processed.
        """
        tickets: list[dict] = []
        page = 1

        while True:
            page_tickets, total_pages = await self.get_closed_tickets_page(owner_id, page=page)
            tickets.extend(page_tickets)
            if len(tickets) >= limit or page >= total_pages:
                break
            page += 1

        return tickets[:limit]

    async def assign_ticket(self, ticket_id: str, owner_id: str) -> HDEApiResult:
        """Assign ticket to the given HDE user id."""
        url = f"{self.base_url}/tickets/{ticket_id}/"
        await _get_rate_limiter().acquire()
        async with self._make_session() as session:
            async with session.put(url, json={"owner_id": int(owner_id)}) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    @staticmethod
    def _ticket_update_body(
        custom_fields: dict[str, str],
        priority_id: str | None = None,
        type_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"custom_fields": custom_fields}
        # type_id=0 («Вопрос») валиден — поэтому `is not None`, не truthiness
        if priority_id is not None:
            body["priority_id"] = int(priority_id)
        if type_id is not None:
            body["type_id"] = int(type_id)
        return body

    async def update_ticket_fields(
        self,
        ticket_id: str,
        custom_fields: dict[str, str],
        priority_id: str | None = None,
        type_id: str | None = None,
    ) -> HDEApiResult:
        """Update custom fields (keys = field IDs as strings) and, optionally,
        the standard priority_id/type_id of a ticket — one PUT for everything."""
        url = f"{self.base_url}/tickets/{ticket_id}/"
        body = self._ticket_update_body(custom_fields, priority_id, type_id)
        await _get_rate_limiter().acquire()
        async with self._make_session() as session:
            async with session.put(url, json=body) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    async def _post(
        self,
        path: str,
        *,
        text: str = "",
        attachments: Sequence[HDEAttachment] = (),
    ) -> HDEApiResult:
        url = f"{self.base_url}{path}"
        payload = self._build_payload(text=text, attachments=attachments)
        await _get_rate_limiter().acquire()
        async with self._make_session() as session:
            async with session.post(url, data=payload) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    async def _put(self, path: str, *, text: str = "") -> HDEApiResult:
        url = f"{self.base_url}{path}"
        payload = {"text": text.strip()}
        await _get_rate_limiter().acquire()
        async with self._make_session() as session:
            async with session.put(url, data=payload) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    async def _delete(self, path: str) -> HDEApiResult:
        url = f"{self.base_url}{path}"
        await _get_rate_limiter().acquire()
        async with self._make_session() as session:
            async with session.delete(url) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    def _build_payload(
        self,
        *,
        text: str,
        attachments: Sequence[HDEAttachment],
    ) -> aiohttp.FormData | dict[str, str]:
        clean_text = (text or "").strip()
        if not attachments:
            return {"text": clean_text}

        form = aiohttp.FormData()
        form.add_field("text", clean_text)
        for attachment in attachments:
            form.add_field(
                "files",
                attachment.content,
                filename=attachment.filename,
                content_type=attachment.content_type or "application/octet-stream",
            )
        return form

    async def _read_response(self, response: aiohttp.ClientResponse) -> Any:
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            return await response.json()
        return await response.text()

    def _extract_error_message(self, data: Any) -> str:
        if isinstance(data, dict):
            for key in ("detail", "message", "error"):
                value = data.get(key)
                if value:
                    return str(value)

            errors = data.get("errors")
            if isinstance(errors, list):
                parts: list[str] = []
                for item in errors:
                    if isinstance(item, dict):
                        part = item.get("details") or item.get("title") or item.get("message")
                        if part:
                            parts.append(str(part))
                    elif item:
                        parts.append(str(item))
                if parts:
                    return "; ".join(parts)
            if errors:
                return str(errors)

        if isinstance(data, str):
            return data.strip()
        return ""

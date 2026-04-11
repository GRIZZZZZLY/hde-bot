from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

import aiohttp

from .config import config

logger = logging.getLogger(__name__)


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

    async def get_ticket_info(self, ticket_id: str) -> HDETicketInfo:
        """Return client and owner identities for a ticket."""
        url = f"{self.base_url}/tickets/{ticket_id}/"
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.get(url) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    raise HDEApiError(self._extract_error_message(data) or f"HDE API error {response.status}")
        raw = data.get("data", data) if isinstance(data, dict) else {}
        client_name = f"{raw.get('user_name', '')} {raw.get('user_lastname', '')}".strip() or "Клиент"
        owner_name = f"{raw.get('owner_name', '')} {raw.get('owner_lastname', '')}".strip() or "Сотрудник"
        return HDETicketInfo(
            client_id=int(raw.get("user_id", 0)),
            client_name=client_name,
            owner_id=int(raw.get("owner_id", 0)),
            owner_name=owner_name,
        )

    async def get_ticket_posts(self, ticket_id: str, limit: int = 20) -> list[HDEPost]:
        """Return up to *limit* posts (newest first from API, returned oldest-first)."""
        url = f"{self.base_url}/tickets/{ticket_id}/posts/"
        params = {"limit": str(limit)}
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.get(url, params=params) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    raise HDEApiError(self._extract_error_message(data) or f"HDE API error {response.status}")
        items = data.get("data", []) if isinstance(data, dict) else []
        posts = [
            HDEPost(
                post_id=int(item.get("id", 0)),
                user_id=int(item.get("user_id", 0)),
                text=item.get("text", ""),
                date_created=item.get("date_created", ""),
            )
            for item in items
            if isinstance(item, dict)
        ]
        posts.reverse()  # oldest first for display
        return posts

    async def get_my_open_tickets(self) -> list[HDETicket]:
        """Return all open/in-progress tickets assigned to me, paginated."""
        all_tickets: list[HDETicket] = []
        page = 1
        owner_id = config.hde_owner_id

        while True:
            url = f"{self.base_url}/tickets/"
            params = {
                "owner_list": owner_id,
                "status_list": "open,process",
                "page": str(page),
            }
            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.get(url, params=params) as response:
                    data = await self._read_response(response)
                    if response.status >= 400:
                        message = self._extract_error_message(data) or f"HDE API error {response.status}"
                        raise HDEApiError(message)

            if not isinstance(data, dict):
                break
            tickets_data = data.get("data", {})
            if not tickets_data:
                break

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

            meta = data.get("meta", {})
            total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
            if page >= total_pages:
                break
            page += 1

        return all_tickets

    async def get_closed_tickets(
        self,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch up to `limit` closed tickets for the given owner_id.

        Returns list of raw ticket dicts from HDE API.
        """
        tickets: list[dict] = []
        page = 1
        per_page = 100  # request max per page to minimise round-trips

        while True:
            url = f"{self.base_url}/tickets/"
            params = {
                "owner_list": owner_id,
                "status_list": "closed",
                "page": str(page),
                "per_page": str(per_page),
            }
            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.get(url, params=params) as response:
                    data = await self._read_response(response)
                    if response.status >= 400:
                        message = (
                            self._extract_error_message(data)
                            or f"HDE API error {response.status}"
                        )
                        raise HDEApiError(message)

            if not isinstance(data, dict):
                break
            logger.info(
                "get_closed_tickets page=%d response keys=%s",
                page, list(data.keys()),
            )
            tickets_data = data.get("data", {})
            if not tickets_data:
                break

            for ticket_raw in tickets_data.values():
                if isinstance(ticket_raw, dict):
                    tickets.append(ticket_raw)
                    if len(tickets) >= limit:
                        return tickets

            meta = data.get("meta", {})
            total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
            logger.info(
                "get_closed_tickets page=%d total_pages=%d fetched_so_far=%d meta=%s",
                page, total_pages, len(tickets), meta,
            )
            if page >= total_pages:
                break
            page += 1

        return tickets

    async def _post(
        self,
        path: str,
        *,
        text: str = "",
        attachments: Sequence[HDEAttachment] = (),
    ) -> HDEApiResult:
        url = f"{self.base_url}{path}"
        payload = self._build_payload(text=text, attachments=attachments)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.post(url, data=payload) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    async def _put(self, path: str, *, text: str = "") -> HDEApiResult:
        url = f"{self.base_url}{path}"
        payload = {"text": text.strip()}
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.put(url, data=payload) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)

    async def _delete(self, path: str) -> HDEApiResult:
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession(auth=self.auth) as session:
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

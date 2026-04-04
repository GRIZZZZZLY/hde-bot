from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Iterable
from urllib.parse import urlparse, unquote

import aiohttp

from .config import config
from .hde_api import HDEAttachment

ATTACHMENT_FIELDS = (
    "attachments",
    "attachments_preview_links",
    "last_answer_attachments",
    "last_answer_attachments_preview_links",
)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class ClientAttachmentRef:
    url: str
    filename: str = ""
    content_type: str = ""
    kind: str = ""


def extract_client_attachment_refs(payload: dict[str, Any]) -> list[ClientAttachmentRef]:
    refs: list[ClientAttachmentRef] = []
    for key in ATTACHMENT_FIELDS:
        refs.extend(_normalize_attachment_value(payload.get(key)))

    unique: list[ClientAttachmentRef] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.url in seen:
            continue
        seen.add(ref.url)
        unique.append(ref)
    return unique


def _normalize_attachment_value(value: Any) -> list[ClientAttachmentRef]:
    if value in (None, "", [], ()):
        return []
    if isinstance(value, ClientAttachmentRef):
        return [value]
    if isinstance(value, dict):
        url = str(
            value.get("url")
            or value.get("href")
            or value.get("preview_url")
            or value.get("link")
            or ""
        ).strip()
        if not url:
            return []
        return [
            ClientAttachmentRef(
                url=url,
                filename=str(value.get("filename") or value.get("name") or "").strip(),
                content_type=str(value.get("content_type") or value.get("mime_type") or "").strip(),
                kind=str(value.get("type") or value.get("kind") or "").strip(),
            )
        ]
    if isinstance(value, (list, tuple, set)):
        refs: list[ClientAttachmentRef] = []
        for item in value:
            refs.extend(_normalize_attachment_value(item))
        return refs
    if isinstance(value, str):
        return _normalize_attachment_string(value)
    return []


def _normalize_attachment_string(value: str) -> list[ClientAttachmentRef]:
    text = unescape(value or "").strip()
    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        try:
            return _normalize_attachment_value(json.loads(text))
        except json.JSONDecodeError:
            pass

    hrefs = [match.strip() for match in HREF_RE.findall(text) if match.strip()]
    if hrefs:
        return [ClientAttachmentRef(url=url, filename=_filename_from_url(url)) for url in hrefs]

    urls = [match.strip() for match in URL_RE.findall(text) if match.strip()]
    if urls:
        return [ClientAttachmentRef(url=url, filename=_filename_from_url(url)) for url in urls]

    refs: list[ClientAttachmentRef] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            refs.append(ClientAttachmentRef(url=line, filename=_filename_from_url(line)))
    return refs


def _filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    return filename or "attachment.bin"


def _auth_for_url(url: str) -> aiohttp.BasicAuth | None:
    if not config.has_hde_api_credentials():
        return None
    api_host = urlparse(config.hde_api_base_url).hostname
    url_host = urlparse(url).hostname
    if api_host and url_host and api_host.lower() == url_host.lower():
        return aiohttp.BasicAuth(config.hde_api_email, config.hde_api_key)
    return None


async def download_client_attachment(ref: ClientAttachmentRef) -> HDEAttachment:
    async with aiohttp.ClientSession(auth=_auth_for_url(ref.url)) as session:
        async with session.get(ref.url) as response:
            if response.status >= 400:
                raise RuntimeError(f"attachment download failed with status {response.status}")
            content = await response.read()
            content_type = ref.content_type or response.headers.get("Content-Type", "").split(";")[0].strip()
    return HDEAttachment(
        filename=ref.filename or _filename_from_url(ref.url),
        content=content,
        content_type=content_type or "application/octet-stream",
    )


def detect_telegram_media_kind(attachment: HDEAttachment) -> str:
    content_type = (attachment.content_type or "").lower()
    filename = attachment.filename.lower()

    if content_type.startswith("image/") or filename.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "photo"
    if content_type.startswith("video/") or filename.endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
        return "video"
    if content_type == "audio/ogg" or filename.endswith(".ogg"):
        return "voice"
    if content_type.startswith("audio/") or filename.endswith((".mp3", ".m4a", ".wav", ".flac", ".aac")):
        return "audio"
    return "document"

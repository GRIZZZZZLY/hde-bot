"""Engineers the bot serves: who they are in HDE and Telegram, and where their topics live.

The primary operator is built from the classic single-operator settings in .env
(HDE_OWNER_ID, HDE_OWNER_NAME, GROUP_CHAT_ID, PERSONAL_CHAT_ID, the shared HDE key),
so with no OPERATORS_FILE the bot behaves exactly as before. Colleagues come from
OPERATORS_FILE (JSON list, chmod 600); each entry points at its own HDE key file
holding "email:api_key".
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Operator:
    hde_id: str
    name: str
    tg_user_id: int
    chat_id: int
    api_auth: str = ""  # "email:api_key"; "" = the shared key from .env
    ai_enabled: bool = False
    # «Я про вас не забыл…» to the client shortly before the SLA, sent as this engineer
    auto_reassurance: bool = False

    @property
    def first_name(self) -> str:
        return (self.name.split() or [""])[0]


def _primary() -> Operator:
    return Operator(
        hde_id=config.hde_owner_id,
        name=config.hde_owner_name,
        tg_user_id=config.personal_chat_id,
        chat_id=config.group_chat_id,
        ai_enabled=True,
        auto_reassurance=True,
    )


def _load_extra(path: str) -> list[Operator]:
    if not path or not Path(path).is_file():
        return []
    extra = []
    for entry in json.loads(Path(path).read_text(encoding="utf-8")):
        key_file = entry.get("key_file", "")
        extra.append(Operator(
            hde_id=str(entry["hde_id"]),
            name=entry["name"],
            tg_user_id=int(entry["tg_user_id"]),
            chat_id=int(entry["chat_id"]),
            api_auth=Path(key_file).read_text(encoding="utf-8").strip() if key_file else "",
            ai_enabled=bool(entry.get("ai_enabled", False)),
            auto_reassurance=bool(entry.get("auto_reassurance", False)),
        ))
    return extra


def _load_colleagues() -> tuple[Operator, ...]:
    try:
        return tuple(_load_extra(os.getenv("OPERATORS_FILE", "secrets/operators.json")))
    except (OSError, ValueError, KeyError) as exc:
        # A broken colleague entry must not take the bot down for the primary operator.
        logger.error("OPERATORS_FILE ignored: %s", exc)
        return ()


COLLEAGUES: tuple[Operator, ...] = _load_colleagues()


def all_operators() -> tuple[Operator, ...]:
    """Primary first. Built on each call so it always follows the live config."""
    first = primary()
    return (first, *(o for o in COLLEAGUES if o.hde_id != first.hde_id))


def primary() -> Operator:
    return _primary()


def by_owner(owner_id: str, owner_name: str = "") -> Operator | None:
    """The operator who owns a ticket. Webhooks sometimes carry only a first name, so id wins."""
    owner_id = str(owner_id or "").strip()
    if owner_id:
        return next((o for o in all_operators() if o.hde_id == owner_id), None)
    name = (owner_name or "").strip().lower()
    if name:
        return next((o for o in all_operators() if o.name.lower() == name), None)
    return None


def by_tg_user(user_id: int) -> Operator | None:
    return next((o for o in all_operators() if o.tg_user_id == user_id), None)


def by_chat(chat_id: int) -> Operator | None:
    return next((o for o in all_operators() if o.chat_id == chat_id), None)


AI_OFF_NOTE = "ИИ для этой группы выключен"


def ai_enabled_for(*, chat_id: int | None = None, owner_id: str | None = None) -> bool:
    """May this ticket spend the shared model quota? Chat wins over owner when both are known."""
    operator = (by_chat(chat_id) if chat_id is not None else None) or (
        by_owner(owner_id) if owner_id else None
    )
    return operator.ai_enabled if operator is not None else False


def hde_auth_for_user(tg_user_id: int) -> str:
    """HDE credentials of whoever pressed the button; "" (shared key) for the primary operator."""
    operator = by_tg_user(tg_user_id)
    return operator.api_auth if operator else ""


def hde_auth_for_owner(owner_id: str) -> str:
    """HDE credentials of the ticket owner, for messages the bot sends on their behalf."""
    operator = by_owner(owner_id)
    return operator.api_auth if operator else ""

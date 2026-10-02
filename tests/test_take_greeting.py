"""Greeting buttons under a General notification: take the ticket and write to the client."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import hde_api
from bot.config import config
from bot.general_channel import _take_keyboard, take_greeting
from bot.handlers.commands import cb_take_ticket


def test_greeting_now():
    assert take_greeting("Игорь", "now") == (
        "Здравствуйте, меня зовут Игорь, инженер по оборудованию. "
        "Изучаю информацию по вашему обращению, вернусь через 5 минут."
    )


@pytest.mark.parametrize("hours,tail", [(1, "1 часа."), (2, "2 часов."), (4, "4 часов."), (11, "11 часов."), (21, "21 часа.")])
def test_greeting_busy_hours_grammar(hours, tail):
    text = take_greeting("Игорь", str(hours))
    assert text.startswith("Здравствуйте! Меня зовут Игорь, инженер по оборудованию.")
    assert text.endswith(f"в течение {tail}")


def test_keyboard_callbacks(monkeypatch):
    monkeypatch.setattr(config, "take_busy_hours", (1, 2, 4))
    rows = _take_keyboard("555").inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [
        ["take:555:now"],
        ["take:555:1", "take:555:2", "take:555:4"],
    ]


class _FakeApi:
    def __init__(self, fail_post: bool = False):
        self.assigned: list[tuple[str, str]] = []
        self.posts: list[tuple[str, str]] = []
        self.fail_post = fail_post

    async def assign_ticket(self, ticket_id, owner_id):
        self.assigned.append((ticket_id, owner_id))

    async def add_post(self, ticket_id, text=""):
        if self.fail_post:
            raise hde_api.HDEApiError("boom")
        self.posts.append((ticket_id, text))


def _callback(data: str):
    message = SimpleNamespace(text="🆕 Неприсвоенный тикет", edit_reply_markup=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


@pytest.fixture
def api(monkeypatch, initialized_db):
    fake = _FakeApi()
    monkeypatch.setattr(hde_api, "HDEApiClient", lambda: fake)
    monkeypatch.setattr(config, "hde_owner_id", "98")
    monkeypatch.setattr(config, "hde_owner_name", "Игорь Кравцов")
    monkeypatch.setattr(config, "public_reply_enabled", True)
    return fake


@pytest.mark.asyncio
async def test_busy_button_assigns_then_greets(api):
    cb = _callback("take:555:2")
    await cb_take_ticket(cb)
    assert api.assigned == [("555", "98")]
    assert api.posts == [("555", take_greeting("Игорь", "2"))]
    assert "вернусь в течение 2 ч" in cb.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_old_take_button_assigns_without_greeting(api):
    await cb_take_ticket(_callback("take:555"))
    assert api.assigned == [("555", "98")]
    assert api.posts == []


@pytest.mark.asyncio
async def test_greeting_failure_keeps_assignment_and_says_so(api):
    api.fail_post = True
    cb = _callback("take:555:now")
    await cb_take_ticket(cb)
    assert api.assigned == [("555", "98")]
    assert "Клиенту не ушло" in cb.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_forged_mode_rejected(api):
    await cb_take_ticket(_callback("take:555:rm"))
    assert api.assigned == [] and api.posts == []

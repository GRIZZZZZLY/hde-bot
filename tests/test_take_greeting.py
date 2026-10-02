"""Greeting buttons under a General notification: take the ticket and write to the client."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import db, hde_api
from bot.config import config
from bot.general_channel import TAKE_HOURS_SETTING, _take_keyboard, parse_busy_options, take_greeting
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


def test_greeting_busy_range():
    assert take_greeting("Игорь", "1-2").endswith("в течение 1–2 часов.")


@pytest.mark.parametrize("text,expected", [
    ("1 1-2 2 4", ["1", "1-2", "2", "4"]),
    ("1, 1–2, 3", ["1", "1-2", "3"]),
    ("", None), ("2-1", None), ("2-2", None), ("0", None), ("73", None),
    ("1 2 3 4 5 6 7", None), ("abc", None),
])
def test_parse_busy_options(text, expected):
    assert parse_busy_options(text) == expected


@pytest.mark.asyncio
async def test_keyboard_uses_saved_options(initialized_db):
    await db.set_setting(TAKE_HOURS_SETTING, "1-2 3")
    rows = (await _take_keyboard("555")).inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [
        ["take:555:now"],
        ["take:555:1-2", "take:555:3"],
    ]
    assert rows[1][0].text == "⏳ 1–2 ч"


@pytest.mark.asyncio
async def test_keyboard_default_options(initialized_db):
    rows = (await _take_keyboard("555")).inline_keyboard
    assert [b.callback_data for b in rows[1]] == ["take:555:1", "take:555:1-2", "take:555:2", "take:555:4"]


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


def _callback(data: str, user_id: int | None = None, chat_id: int | None = None):
    message = SimpleNamespace(
        text="🆕 Неприсвоенный тикет", edit_reply_markup=AsyncMock(), edit_text=AsyncMock(),
        chat=SimpleNamespace(id=config.group_chat_id if chat_id is None else chat_id),
    )
    user = SimpleNamespace(id=config.personal_chat_id if user_id is None else user_id)
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    return SimpleNamespace(data=data, message=message, answer=AsyncMock(), from_user=user, bot=bot)


@pytest.fixture
def api(monkeypatch, initialized_db):
    fake = _FakeApi()
    fake.auths = []

    def _client(auth=""):
        fake.auths.append(auth)
        return fake

    monkeypatch.setattr(hde_api, "HDEApiClient", _client)
    monkeypatch.setattr(config, "hde_owner_id", "98")
    monkeypatch.setattr(config, "hde_owner_name", "Игорь Кравцов")
    monkeypatch.setattr(config, "public_reply_enabled", True)
    return fake


@pytest.fixture(autouse=True)
async def _posted_in_general(initialized_db):
    """Ticket 555 is announced in General of the primary group, as before a press."""
    await db.save_general_message("555", 70, "Касса", config.group_chat_id)


@pytest.mark.asyncio
async def test_busy_button_assigns_then_greets(api):
    cb = _callback("take:555:1-2")
    await cb_take_ticket(cb)
    assert api.assigned == [("555", "98")]
    assert api.posts == [("555", take_greeting("Игорь", "1-2"))]
    assert "вернусь в течение 1–2 ч" in cb.message.edit_text.call_args.args[0]


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


@pytest.mark.asyncio
async def test_taketimes_command_saves_and_rejects(monkeypatch, initialized_db):
    from bot.handlers.commands import cmd_taketimes
    monkeypatch.setattr(config, "operator_telegram_user_ids", (1,))

    def msg(user_id):
        return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())

    ok = msg(1)
    await cmd_taketimes(ok, SimpleNamespace(args="1-2 3 6"))
    assert await db.get_setting(TAKE_HOURS_SETTING) == "1-2 3 6"
    assert "1–2 ч · 3 ч · 6 ч" in ok.answer.call_args.args[0]

    await cmd_taketimes(msg(1), SimpleNamespace(args="9-1"))
    await cmd_taketimes(msg(2), SimpleNamespace(args="5"))  # not an operator
    assert await db.get_setting(TAKE_HOURS_SETTING) == "1-2 3 6"


@pytest.mark.asyncio
async def test_colleague_takes_ticket_with_own_name_and_key(api, monkeypatch):
    from bot import operators
    maxim = operators.Operator("102", "Максим Яницкий", 1220214456, -100222, api_auth="m@x:key")
    monkeypatch.setattr(operators, "COLLEAGUES", (maxim,))
    cb = _callback("take:555:now", user_id=1220214456)
    await cb_take_ticket(cb)
    assert api.assigned == [("555", "102")]
    assert api.posts == [("555", take_greeting("Максим", "now"))]
    assert set(api.auths) == {"m@x:key"}  # both the assignment and the greeting use his key
    assert "Забрал Максим Яницкий" in cb.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_stranger_cannot_take(api):
    cb = _callback("take:555:now", user_id=999)
    await cb_take_ticket(cb)
    assert api.assigned == [] and api.posts == []
    cb.message.edit_reply_markup.assert_not_awaited()  # buttons stay for the engineers


@pytest.mark.asyncio
async def test_first_press_wins_across_groups(api, monkeypatch):
    from bot import operators
    maxim = operators.Operator("102", "Максим Яницкий", 1220214456, -100222, api_auth="m@x:key")
    monkeypatch.setattr(operators, "COLLEAGUES", (maxim,))
    await db.save_general_message("555", 71, "Касса", -100222)

    first = _callback("take:555:2", user_id=1220214456, chat_id=-100222)
    await cb_take_ticket(first)
    # the primary group's copy now says who took it, and has no buttons
    edit = first.bot.edit_message_text.call_args.kwargs
    assert (edit["chat_id"], edit["message_id"]) == (config.group_chat_id, 70)
    assert "Забрал Максим Яницкий" in edit["text"] and "reply_markup" not in edit
    assert await db.list_general_messages_for("555") == []

    late = _callback("take:555:now")  # the primary engineer presses a second later
    await cb_take_ticket(late)
    assert api.assigned == [("555", "102")]  # still Maxim's
    assert "уже забрали" in late.answer.call_args.args[0]

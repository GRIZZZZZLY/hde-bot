import pytest

from bot.command_menu import (
    ARGS_HELP_TEXT,
    DM_COMMANDS,
    GROUP_COMMANDS,
    HIDDEN_COMMANDS,
    build_command_scopes,
    hub_keyboard,
    submenu_keyboard,
)


def test_command_sets_partitioned():
    dm = {c.command for c in DM_COMMANDS}
    grp = {c.command for c in GROUP_COMMANDS}
    assert dm == {
        "menu", "help", "status", "refresh", "vacation",
        "workon", "report", "weekly", "digest", "aisummary", "taketimes",
    }
    assert grp == {"note", "send", "delete", "autofill", "help"}
    # Regression guard: this redesign removes /start and the /aistatus
    # alias — they must never reappear in any menu set.
    assert "start" not in dm | grp | set(HIDDEN_COMMANDS)
    assert "aistatus" not in dm | grp | set(HIDDEN_COMMANDS)
    assert set(HIDDEN_COMMANDS) == {
        "aiknowledge", "aimetrics", "aiimport", "aireindex",
        "aibackfill", "aianalyze", "aioptimize", "promptrollback",
    }
    assert (dm | grp) & set(HIDDEN_COMMANDS) == set()


def test_build_command_scopes_shape():
    scopes = build_command_scopes([-100123])
    kinds = {s["scope"].type for s in scopes}
    assert "all_private_chats" in kinds
    assert "chat" in kinds
    for entry in scopes:
        assert entry["commands"]
        assert all(hasattr(c, "command") for c in entry["commands"])


# ---------------------------------------------------------------------------
# Task 2: inline hub keyboards + args help text
# ---------------------------------------------------------------------------


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_hub_keyboard_root():
    kb = hub_keyboard()
    cbs = _callbacks(kb)
    assert "menu:status" in cbs
    assert "menu:refresh" in cbs
    assert "menu:quiet" in cbs
    assert "menu:reports" in cbs
    assert "menu:ai" in cbs
    assert "menu:args" in cbs


def test_submenu_keyboards_have_back():
    for name in ("quiet", "reports", "ai"):
        kb = submenu_keyboard(name)
        cbs = _callbacks(kb)
        assert "menu:root" in cbs
        assert cbs, f"{name} submenu empty"


def test_submenu_actions():
    assert "menu:vacation" in _callbacks(submenu_keyboard("quiet"))
    assert "menu:workon" in _callbacks(submenu_keyboard("quiet"))
    assert "menu:report_yesterday" in _callbacks(submenu_keyboard("reports"))
    assert "menu:digest" in _callbacks(submenu_keyboard("reports"))
    assert "menu:aisummary" in _callbacks(submenu_keyboard("ai"))
    assert "menu:aimetrics" in _callbacks(submenu_keyboard("ai"))


def test_args_help_lists_arg_variants():
    for frag in ("/vacation 3d", "/report ", "/aisummary on", "/aiimport"):
        assert frag in ARGS_HELP_TEXT


def test_unknown_submenu_raises():
    with pytest.raises(KeyError):
        submenu_keyboard("nope")


@pytest.mark.asyncio
async def test_menu_callback_status(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import bot.handlers.commands as cmds

    monkeypatch.setattr(cmds, "count_active_topics", AsyncMock(return_value=3))
    monkeypatch.setattr(cmds, "count_pending_delete_topics", AsyncMock(return_value=1))
    monkeypatch.setattr(cmds, "count_pending_pre_sla_topics", AsyncMock(return_value=2))
    monkeypatch.setattr(cmds, "count_total_topics", AsyncMock(return_value=9))

    cb = MagicMock()
    cb.data = "menu:status"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()

    await cmds.cb_menu(cb)

    cb.answer.assert_awaited()
    sent = " ".join(
        str(c.args[0]) for c in
        list(cb.message.edit_text.await_args_list) + list(cb.message.answer.await_args_list)
    )
    assert "Активных topics" in sent and "3" in sent


@pytest.mark.asyncio
async def test_menu_callback_navigation():
    from unittest.mock import AsyncMock, MagicMock
    import bot.handlers.commands as cmds
    cb = MagicMock()
    cb.data = "menu:quiet"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    await cmds.cb_menu(cb)
    cb.message.edit_text.assert_awaited()

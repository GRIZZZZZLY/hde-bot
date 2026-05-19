from bot.command_menu import (
    DM_COMMANDS,
    GROUP_COMMANDS,
    HIDDEN_COMMANDS,
    build_command_scopes,
)


def test_command_sets_partitioned():
    dm = {c.command for c in DM_COMMANDS}
    grp = {c.command for c in GROUP_COMMANDS}
    assert dm == {
        "menu", "help", "status", "refresh", "vacation",
        "workon", "report", "digest", "aisummary",
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
    scopes = build_command_scopes(group_chat_id=-100123)
    kinds = {s["scope"].type for s in scopes}
    assert "all_private_chats" in kinds
    assert "chat" in kinds
    for entry in scopes:
        assert entry["commands"]
        assert all(hasattr(c, "command") for c in entry["commands"])


# ---------------------------------------------------------------------------
# Task 2: inline hub keyboards + args help text
# ---------------------------------------------------------------------------

from bot.command_menu import hub_keyboard, submenu_keyboard, ARGS_HELP_TEXT


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
    import pytest
    with pytest.raises(KeyError):
        submenu_keyboard("nope")

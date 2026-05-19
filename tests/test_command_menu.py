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

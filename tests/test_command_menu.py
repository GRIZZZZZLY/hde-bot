from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.command_menu import (
    DM_COMMANDS,
    GROUP_COMMANDS,
    HIDDEN_COMMANDS,
    MAINT_COMMANDS,
    ai_keyboard,
    build_command_scopes,
    confirm_keyboard,
    days_keyboard,
    hours_keyboard,
    hub_keyboard,
    last_sundays,
    maint_keyboard,
    menu_reply_keyboard,
    quiet_keyboard,
    recent_work_days,
    reports_keyboard,
    toggle_hour,
    weeks_keyboard,
)


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_only_menu_is_advertised():
    assert [c.command for c in DM_COMMANDS] == ["menu"]
    assert [c.command for c in GROUP_COMMANDS] == ["menu"]
    assert "start" not in set(HIDDEN_COMMANDS)
    assert set(HIDDEN_COMMANDS) == {
        "aiknowledge", "aimetrics", "aiimport", "aireindex",
        "aibackfill", "aianalyze", "aioptimize", "promptrollback",
    }


def test_build_command_scopes_shape():
    scopes = build_command_scopes([-100123])
    kinds = {s["scope"].type for s in scopes}
    assert kinds == {"all_private_chats", "chat"}


def test_menu_reply_keyboard_is_persistent():
    kb = menu_reply_keyboard()
    assert kb.keyboard[0][0].text == "☰ Меню"
    assert kb.is_persistent and kb.resize_keyboard


def test_hub_has_every_screen():
    cbs = _callbacks(hub_keyboard())
    for data in ("menu:status", "menu:refresh", "menu:quiet", "menu:reports",
                 "menu:ai", "menu:hours", "menu:howto"):
        assert data in cbs
    assert "menu:args" not in cbs


def test_quiet_keyboard_shows_workon_only_when_active():
    assert "menu:workon" not in _callbacks(quiet_keyboard(active=False))
    cbs = _callbacks(quiet_keyboard(active=True))
    assert {"menu:vac:next", "menu:vac:1", "menu:vac:3", "menu:vac:7", "menu:workon", "menu:root"} <= set(cbs)


def test_reports_keyboard():
    cbs = _callbacks(reports_keyboard())
    assert {"menu:report_yesterday", "menu:days", "menu:weeks", "menu:digest", "menu:root"} <= set(cbs)


def test_recent_work_days_skips_days_off_and_today():
    # 2026-10-13 is a Tuesday; work days Mon-Fri
    days = recent_work_days(date(2026, 10, 13), (0, 1, 2, 3, 4))
    assert days[0] == date(2026, 10, 12)          # Monday, never today
    assert date(2026, 10, 11) not in days          # Sunday skipped
    assert len(days) == 7
    assert all(d.weekday() < 5 for d in days)


def test_days_keyboard_labels_and_data():
    kb = days_keyboard(date(2026, 10, 13), (0, 1, 2, 3, 4))
    assert "menu:rep:2026-10-12" in _callbacks(kb)
    assert "пн 12.10" in _texts(kb)
    assert "menu:reports" in _callbacks(kb)


def test_last_sundays():
    assert last_sundays(date(2026, 10, 13)) == [date(2026, 10, 11), date(2026, 10, 4), date(2026, 9, 27)]
    assert last_sundays(date(2026, 10, 11))[0] == date(2026, 10, 11)   # Sunday itself


def test_weeks_keyboard():
    cbs = _callbacks(weeks_keyboard(date(2026, 10, 13)))
    assert cbs[:3] == ["menu:week:2026-10-11", "menu:week:2026-10-04", "menu:week:2026-09-27"]


def test_ai_keyboard_label_shows_state():
    assert "вкл" in _texts(ai_keyboard(True))[0]
    assert "выкл" in _texts(ai_keyboard(False))[0]
    assert {"menu:aisummary", "menu:aimetrics", "menu:aiknowledge", "menu:maint"} <= set(_callbacks(ai_keyboard(True)))


def test_maint_and_confirm():
    cbs = _callbacks(maint_keyboard())
    assert [f"menu:ask:{c}" for c in MAINT_COMMANDS] == cbs[:-1]
    assert _callbacks(confirm_keyboard("aiimport")) == ["menu:run:aiimport", "menu:maint"]
    assert all(len(d.encode()) <= 64 for d in cbs)


def test_hours_keyboard_marks_selected():
    texts = _texts(hours_keyboard(["1", "1-2"]))
    assert "✅ 1 ч" in texts and "✅ 1–2 ч" in texts and "4 ч" in texts


def test_hours_keyboard_keeps_custom_option():
    kb = hours_keyboard(["5", "1"])
    assert "menu:hour:5" in _callbacks(kb)
    assert "✅ 5 ч" in _texts(kb)


@pytest.mark.parametrize("selected,option,expected", [
    (["1", "4"], "2", ["1", "2", "4"]),          # added in grid order
    (["1", "4"], "4", ["1"]),                    # removed
    (["1"], "1", None),                          # cannot drop the last one
    (["1", "1-2", "2", "3", "4", "6"], "8", None),  # cannot pick a 7th
    (["5", "1"], "2", ["1", "2", "5"]),          # custom option kept, after the grid
])
def test_toggle_hour(selected, option, expected):
    assert toggle_hour(selected, option) == expected


# --- shared bodies used by both commands and menu buttons -------------------

async def test_status_text_has_metrics(monkeypatch):
    import bot.handlers.commands as cmds
    for name in ("count_active_topics", "count_pending_delete_topics",
                 "count_pending_pre_sla_topics", "count_total_topics"):
        monkeypatch.setattr(cmds, name, AsyncMock(return_value=2))
    text = await cmds.status_text()
    assert "Активных topics" in text and "Метрики" in text


def test_vacation_until_from_args(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.work_schedule as ws
    now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    marker = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ws, "next_work_start", lambda: marker)
    assert cmds.vacation_until_from_args("", now) == marker
    assert cmds.vacation_until_from_args("3d", now) == now + timedelta(days=3)
    assert cmds.vacation_until_from_args("2026-10-20", now) == datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    assert cmds.vacation_until_from_args("soon", now) is None


async def test_run_daily_report_passes_date(monkeypatch):
    import bot.handlers.report_commands as rc
    import bot.reporting.runner as runner
    from bot.config import config
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    run = AsyncMock(return_value="ok")
    monkeypatch.setattr(runner, "run_report", run)
    monkeypatch.setattr(rc, "mark_report_sent", AsyncMock())
    monkeypatch.setattr(config, "work_days", (0, 1, 2, 3, 4))
    target = MagicMock()
    target.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    await rc.run_daily_report(target, date(2026, 10, 12))
    run.assert_awaited_once_with(date(2026, 10, 12))


async def test_show_promptrollback_has_no_auth_gate(monkeypatch):
    import bot.handlers.optimizer_commands as oc
    import bot.db as db_module
    monkeypatch.setattr(db_module, "list_prompt_versions", AsyncMock(return_value=[]))
    msg = MagicMock()
    msg.from_user = MagicMock(id=999)            # not an admin, and it does not matter here
    msg.answer = AsyncMock()
    await oc.show_promptrollback(msg)
    msg.answer.assert_awaited_once()


# --- menu handlers ----------------------------------------------------------

def _callback(data, user_id=123456789):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=user_id)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


async def test_menu_status_button_matches_command(monkeypatch):
    import bot.handlers.commands as cmds
    monkeypatch.setattr(cmds, "status_text", AsyncMock(return_value="STATUS"))
    cb = _callback("menu:status")
    await cmds.cb_menu(cb)
    cb.message.answer.assert_awaited_once_with("STATUS", parse_mode="HTML")


@pytest.mark.parametrize("screen", ["root", "quiet", "reports", "days", "weeks", "ai", "maint", "hours", "howto"])
async def test_every_screen_renders(initialized_db, screen):
    import bot.handlers.commands as cmds
    cb = _callback(f"menu:{screen}")
    await cmds.cb_menu(cb)
    cb.message.edit_text.assert_awaited_once()


async def test_vacation_buttons(monkeypatch, initialized_db):
    import bot.handlers.commands as cmds
    seen = []
    monkeypatch.setattr(cmds, "turn_on_vacation", AsyncMock(side_effect=lambda t, until: seen.append(until)))
    cb = _callback("menu:vac:3")
    before = datetime.now(timezone.utc)
    await cmds.cb_menu(cb)
    assert timedelta(days=3) <= seen[0] - before < timedelta(days=3, minutes=1)


async def test_report_day_button(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.report_commands as rc
    run = AsyncMock()
    monkeypatch.setattr(rc, "run_daily_report", run)
    cb = _callback("menu:rep:2026-10-06")
    await cmds.cb_menu(cb)
    run.assert_awaited_once_with(cb.message, date(2026, 10, 6))


async def test_week_button(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.report_commands as rc
    run = AsyncMock()
    monkeypatch.setattr(rc, "run_weekly", run)
    cb = _callback("menu:week:2026-10-11")
    await cmds.cb_menu(cb)
    run.assert_awaited_once_with(cb.message, date(2026, 10, 11))


async def test_hour_toggle_saves_and_rejects(initialized_db):
    import bot.handlers.commands as cmds
    from bot.db import get_setting, set_setting
    from bot.general_channel import TAKE_HOURS_SETTING
    await set_setting(TAKE_HOURS_SETTING, "1 4")
    await cmds.cb_menu(_callback("menu:hour:2"))
    assert await get_setting(TAKE_HOURS_SETTING) == "1 2 4"
    await set_setting(TAKE_HOURS_SETTING, "1")
    cb = _callback("menu:hour:1")
    await cmds.cb_menu(cb)
    assert await get_setting(TAKE_HOURS_SETTING) == "1"
    cb.answer.assert_awaited_with("От 1 до 6 вариантов", show_alert=True)


async def test_hour_toggle_ignores_garbage(initialized_db):
    import bot.handlers.commands as cmds
    from bot.db import get_setting, set_setting
    from bot.general_channel import TAKE_HOURS_SETTING
    await set_setting(TAKE_HOURS_SETTING, "1 4")
    await cmds.cb_menu(_callback("menu:hour:99"))
    assert await get_setting(TAKE_HOURS_SETTING) == "1 4"


async def test_maint_run_only_whitelisted(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.ai_commands as ai
    reindex = AsyncMock()
    monkeypatch.setattr(ai, "cmd_aireindex", reindex)
    await cmds.cb_menu(_callback("menu:run:rm-rf"))
    reindex.assert_not_awaited()
    await cmds.cb_menu(_callback("menu:run:aireindex"))
    reindex.assert_awaited_once()


async def test_maint_optimizer_needs_admin(monkeypatch):
    import bot.handlers.commands as cmds
    import bot.handlers.optimizer_commands as oc
    run = AsyncMock()
    monkeypatch.setattr(oc, "run_aioptimize", run)
    await cmds.cb_menu(_callback("menu:run:aioptimize", user_id=555))
    run.assert_not_awaited()
    await cmds.cb_menu(_callback("menu:run:aioptimize", user_id=123456789))
    run.assert_awaited_once()


async def test_start_in_private_sets_menu_keyboard_once(initialized_db):
    import bot.handlers.commands as cmds
    msg = MagicMock()
    msg.chat = MagicMock(id=42, type="private")
    msg.answer = AsyncMock()
    await cmds.cmd_menu(msg)
    await cmds.cmd_menu(msg)
    reply_kb_sends = [c for c in msg.answer.await_args_list
                      if type(c.kwargs.get("reply_markup")).__name__ == "ReplyKeyboardMarkup"]
    assert len(reply_kb_sends) == 1
    assert msg.answer.await_count == 3        # keyboard note once + hub twice


async def test_menu_in_group_has_no_reply_keyboard(initialized_db):
    import bot.handlers.commands as cmds
    msg = MagicMock()
    msg.chat = MagicMock(id=-100, type="supergroup")
    msg.answer = AsyncMock()
    await cmds.cmd_menu(msg)
    assert msg.answer.await_count == 1


async def test_run_daily_report_without_date_reports_the_day_it_marks(monkeypatch):
    """Final review F1: on Monday the button must report Friday, not Sunday, and mark Friday."""
    import bot.handlers.report_commands as rc
    import bot.reporting.runner as runner
    import bot.work_schedule as ws
    from bot.config import config
    monkeypatch.setattr(runner, "is_report_configured", lambda: True)
    run = AsyncMock(return_value="ok")
    monkeypatch.setattr(runner, "run_report", run)
    mark = AsyncMock()
    monkeypatch.setattr(rc, "mark_report_sent", mark)
    monkeypatch.setattr(config, "work_days", (0, 1, 2, 3, 4))
    monkeypatch.setattr(ws, "last_work_day", lambda: date(2026, 10, 9))
    target = MagicMock()
    target.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    await rc.run_daily_report(target, None)
    run.assert_awaited_once_with(date(2026, 10, 9))
    mark.assert_awaited_once_with(date(2026, 10, 9))

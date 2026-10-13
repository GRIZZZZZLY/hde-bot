"""Command-menu UI: scoped command lists, every menu keyboard and menu texts.

Single source of truth for which slash-commands are advertised and for every
menu screen. All actions live behind buttons, so only /menu is advertised;
the old commands still work when typed.
"""
from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

DM_COMMANDS: list[BotCommand] = [BotCommand(command="menu", description="Меню бота")]
GROUP_COMMANDS: list[BotCommand] = [BotCommand(command="menu", description="Меню бота")]

# Callable by typing and from 🛠 Обслуживание, never shown in a command menu
HIDDEN_COMMANDS: list[str] = [
    "aiknowledge", "aimetrics", "aiimport", "aireindex",
    "aibackfill", "aianalyze", "aioptimize", "promptrollback",
]


def build_command_scopes(chat_ids: list[int]) -> list[dict]:
    """Return [{scope, commands}] for bot.set_my_commands: private chats + every engineer's group."""
    return [
        {
            "scope": BotCommandScopeAllPrivateChats(),
            "commands": DM_COMMANDS,
        },
        *(
            {"scope": BotCommandScopeChat(chat_id=chat_id), "commands": GROUP_COMMANDS}
            for chat_id in chat_ids
        ),
    ]


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

MENU_BUTTON_TEXT = "☰ Меню"


def menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """The ☰ button that stays under the input field in a private chat."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


_BACK = [("⬅️ Назад", "menu:root")]


def hub_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📊 Статус", "menu:status"), ("🔄 Синхрон", "menu:refresh")],
        [("💤 Тишина ▸", "menu:quiet"), ("📈 Отчёты ▸", "menu:reports")],
        [("🤖 AI ▸", "menu:ai"), ("⏳ Часы «Беру» ▸", "menu:hours")],
        [("❓ Как отвечать из топика", "menu:howto")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return _kb([_BACK])


def quiet_keyboard(active: bool) -> InlineKeyboardMarkup:
    rows = [
        [("До след. раб. дня", "menu:vac:next")],
        [("1 д", "menu:vac:1"), ("3 д", "menu:vac:3"), ("7 д", "menu:vac:7")],
    ]
    if active:
        rows.append([("▶️ Снять тишину", "menu:workon")])
    rows.append(_BACK)
    return _kb(rows)


def reports_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📈 Отчёт за прошлый раб. день", "menu:report_yesterday")],
        [("📅 Другой день ▸", "menu:days"), ("🗓 Неделя ▸", "menu:weeks")],
        [("🌅 Утренняя сводка", "menu:digest")],
        _BACK,
    ])


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def recent_work_days(today: date, work_days, count: int = 7) -> list[date]:
    """The last `count` work days before `today` (today itself excluded), newest first."""
    days: list[date] = []
    day = today - timedelta(days=1)
    while len(days) < count and day > today - timedelta(days=31):
        if day.weekday() in work_days:
            days.append(day)
        day -= timedelta(days=1)
    return days


def days_keyboard(today: date, work_days) -> InlineKeyboardMarkup:
    buttons = [
        (f"{_WEEKDAYS[d.weekday()]} {d:%d.%m}", f"menu:rep:{d.isoformat()}")
        for d in recent_work_days(today, work_days)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("⬅️ Назад", "menu:reports")])
    return _kb(rows)


def last_sundays(today: date, count: int = 3) -> list[date]:
    """Most recent Sunday on or before `today`, then the ones before it.

    run_weekly_summary(sunday) covers sunday-7 .. sunday-3, i.e. the work week
    that ended before that Sunday.
    """
    last = today - timedelta(days=(today.weekday() + 1) % 7)
    return [last - timedelta(weeks=i) for i in range(count)]


_WEEK_LABELS = ("Прошлая", "Позапрошлая", "3 недели назад")


def weeks_keyboard(today: date) -> InlineKeyboardMarkup:
    rows = [
        [(f"{label}: {s - timedelta(days=7):%d.%m}–{s - timedelta(days=3):%d.%m}",
          f"menu:week:{s.isoformat()}")]
        for label, s in zip(_WEEK_LABELS, last_sundays(today))
    ]
    rows.append([("⬅️ Назад", "menu:reports")])
    return _kb(rows)


def ai_keyboard(summary_on: bool) -> InlineKeyboardMarkup:
    return _kb([
        [(f"🧠 Саммари: {'вкл ✅' if summary_on else 'выкл ❌'}", "menu:aisummary")],
        [("📚 База знаний", "menu:aimetrics"), ("📊 Статистика", "menu:aiknowledge")],
        [("🛠 Обслуживание ▸", "menu:maint")],
        _BACK,
    ])


MAINT_COMMANDS: dict[str, str] = {
    "aiimport": "📥 Импорт тикетов",
    "aireindex": "🔁 Переиндексация",
    "aibackfill": "🏢 Дозаполнить организации",
    "aianalyze": "🔍 Анализ паттернов",
    "aioptimize": "✨ Оптимизация промпта",
    "promptrollback": "↩️ Откат промпта",
}


def maint_keyboard() -> InlineKeyboardMarkup:
    rows = [[(label, f"menu:ask:{cmd}")] for cmd, label in MAINT_COMMANDS.items()]
    rows.append([("⬅️ Назад", "menu:ai")])
    return _kb(rows)


def confirm_keyboard(cmd: str) -> InlineKeyboardMarkup:
    return _kb([[("✅ Да", f"menu:run:{cmd}"), ("Отмена", "menu:maint")]])


HOUR_GRID: tuple[str, ...] = ("1", "1-2", "2", "3", "4", "6", "8", "24")


def _hour_order(selected: list[str]) -> list[str]:
    """Grid options, then any custom option already saved (e.g. «5» set by /taketimes)."""
    return list(HOUR_GRID) + [o for o in selected if o not in HOUR_GRID]


def hours_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        (("✅ " if o in selected else "") + o.replace("-", "–") + " ч", f"menu:hour:{o}")
        for o in _hour_order(selected)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append(_BACK)
    return _kb(rows)


def toggle_hour(selected: list[str], option: str) -> list[str] | None:
    """Selection after tapping `option`, in grid order; None if it would leave 0 or more than 6."""
    chosen = set(selected) ^ {option}
    new = [o for o in _hour_order(selected) if o in chosen]
    return new if 1 <= len(new) <= 6 else None


HUB_TITLE = "🤖 <b>Меню бота</b>\nВыберите действие:"

HOWTO_TEXT = (
    "❓ <b>Как отвечать из топика тикета</b>\n\n"
    "1. Напиши в топик текст, фото, альбом или файл — как обычно.\n"
    "2. Бот спросит «Куда отправить?»: 📤 Клиенту, 💬 Комментарий или ✖️.\n"
    "3. После отправки кнопка 🗑 удалит сообщение из HDE (спросит подтверждение).\n\n"
    "• Исправил своё сообщение в Telegram — оно исправится и в HDE.\n"
    "• Голосовое — заметка по звонку в контекст тикета, клиенту не уходит.\n"
    "• 🪄 Автополя — под последним сообщением клиента, рядом с 💡.\n\n"
    "<i>Команды /send, /note, /delete, /autofill тоже работают.</i>"
)

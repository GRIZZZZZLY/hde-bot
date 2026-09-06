"""Command-menu UI: scoped command lists + inline hub keyboards.

Single source of truth for which slash-commands are advertised where.
Rare AI-maintenance commands stay callable by typing but are listed in
HIDDEN_COMMANDS and never registered in a Telegram command menu.
"""
from __future__ import annotations

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# DM / admin menu (private chats)
DM_COMMANDS: list[BotCommand] = [
    BotCommand(command="menu",     description="Меню бота"),
    BotCommand(command="help",     description="Меню и справка"),
    BotCommand(command="status",   description="Активные топики и pre-SLA"),
    BotCommand(command="refresh",  description="Синхронизировать топики с HDE"),
    BotCommand(command="vacation", description="Режим тишины (напр. /vacation 3d)"),
    BotCommand(command="workon",   description="Снять режим тишины"),
    BotCommand(command="report",   description="Отчёт в Google Sheets (за вчера)"),
    BotCommand(command="weekly",   description="Сводка за прошлую рабочую неделю"),
    BotCommand(command="digest",   description="Вызвать утреннюю сводку"),
    BotCommand(command="aisummary", description="Вкл/выкл AI саммари тикета"),
]

# Operator group / topic menu
GROUP_COMMANDS: list[BotCommand] = [
    BotCommand(command="note",     description="Внутренний комментарий в HDE"),
    BotCommand(command="send",     description="Публичный ответ клиенту через HDE"),
    BotCommand(command="delete",   description="Удалить сообщение из HDE"),
    BotCommand(command="autofill", description="Заполнить Окружение/Классификацию/Роль"),
    BotCommand(command="help",     description="Меню и справка"),
]

# Callable by typing, never shown in a menu
HIDDEN_COMMANDS: list[str] = [
    "aiknowledge", "aimetrics", "aiimport", "aireindex",
    "aibackfill", "aianalyze", "aioptimize", "promptrollback",
]


def build_command_scopes(group_chat_id: int) -> list[dict]:
    """Return [{scope, commands}] for bot.set_my_commands per scope."""
    return [
        {
            "scope": BotCommandScopeAllPrivateChats(),
            "commands": DM_COMMANDS,
        },
        {
            "scope": BotCommandScopeChat(chat_id=group_chat_id),
            "commands": GROUP_COMMANDS,
        },
    ]


# ---------------------------------------------------------------------------
# Inline hub keyboards
# ---------------------------------------------------------------------------

def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


def hub_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📊 Статус", "menu:status"), ("🔄 Синхрон", "menu:refresh")],
        [("💤 Тишина ▸", "menu:quiet"), ("📈 Отчёты ▸", "menu:reports")],
        [("🤖 AI ▸", "menu:ai")],
        [("ℹ️ Команды с аргументами", "menu:args")],
    ])


_SUBMENUS: dict[str, list[list[tuple[str, str]]]] = {
    "quiet": [
        [("💤 Тишина до след. раб. дня", "menu:vacation")],
        [("▶️ Снять тишину", "menu:workon")],
        [("⬅️ Назад", "menu:root")],
    ],
    "reports": [
        [("📈 Отчёт за вчера", "menu:report_yesterday")],
        [("🌅 Утренняя сводка", "menu:digest")],
        [("⬅️ Назад", "menu:root")],
    ],
    "ai": [
        [("🧠 AI summary вкл/выкл", "menu:aisummary")],
        [("📚 База знаний", "menu:aimetrics")],
        [("⬅️ Назад", "menu:root")],
    ],
}


def submenu_keyboard(name: str) -> InlineKeyboardMarkup:
    return _kb(_SUBMENUS[name])  # KeyError on unknown name (caller guards)


def back_keyboard() -> InlineKeyboardMarkup:
    return _kb([[("⬅️ Назад", "menu:root")]])


HUB_TITLE = "🤖 <b>Меню бота</b>\nВыберите действие:"

ARGS_HELP_TEXT = (
    "ℹ️ <b>Команды с аргументами</b>\n\n"
    "<b>Тишина</b>\n"
    "/vacation 3d — режим тишины на N дней\n"
    "/vacation 2026-05-30 — тишина до даты\n\n"
    "<b>Отчёты</b>\n"
    "/report 2026-05-18 — отчёт за конкретную дату\n"
    "/weekly 2026-09-06 — недельная сводка на конкретное воскресенье\n\n"
    "<b>AI</b>\n"
    "/aisummary on | /aisummary off — вкл/выкл AI-саммари\n"
    "/aiknowledge — статистика базы знаний\n"
    "/aimetrics — управление базой знаний\n"
    "/aiimport [N] [owner_id] — импорт закрытых тикетов HDE\n"
    "/aireindex — переиндексировать embeddings\n"
    "/aibackfill — дозаполнить организации\n"
    "/aianalyze — извлечь паттерны решений\n"
    "/aioptimize — ручная оптимизация промпта\n"
    "/promptrollback — откатить версию промпта\n\n"
    "<b>В топике тикета</b>\n"
    "/note текст | /send текст | /delete (в ответ) | /autofill"
)

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

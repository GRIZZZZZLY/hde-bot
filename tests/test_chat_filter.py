"""The bot ignores updates from chats it does not serve (e.g. a colleague's group it was added to)."""
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from bot.config import config
from bot.main import _build_dispatcher


def _update(chat_id: int) -> Update:
    chat_type = "private" if chat_id > 0 else "supergroup"
    return Update(update_id=1, message=Message(
        message_id=1, date=datetime.now(), text="hello",
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=555, is_bot=False, first_name="X"),
    ))


@pytest.mark.asyncio
async def test_unknown_chats_are_dropped(monkeypatch):
    monkeypatch.setattr(config, "group_chat_id", -100111)
    monkeypatch.setattr(config, "personal_chat_id", 777)
    monkeypatch.setattr(config, "operator_telegram_user_ids", (777,))
    bot = Bot(token="42:TEST")
    dp = _build_dispatcher(bot)  # routers attach once per process
    seen = []

    async def recorder(handler, event, data):
        seen.append(event.chat.id)

    dp.message.outer_middleware(recorder)
    cases = {
        -100111: True,   # our group
        777: True,       # operator's private chat
        -100999: False,  # someone else's group
        888: False,      # a stranger's private chat
    }
    for chat_id in cases:
        await dp.feed_update(bot, _update(chat_id))
    await bot.session.close()
    assert seen == [c for c, ok in cases.items() if ok]

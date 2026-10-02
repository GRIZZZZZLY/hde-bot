"""Звонок как контекст тикета, а не как строка в общей базе знаний.

Оператор решает половину обращений голосом. До этого транскрипт звонка уходил
в knowledge_items с source='transcription' — то есть в общий retrieval, откуда
мог всплыть в чужом тикете, — и к своему тикету не привязывался вовсе. Черновик
переспрашивал то, что в звонке уже выяснили, а ночная сверка считала это
ошибкой бота.
"""
from types import SimpleNamespace

import bot.config as config_module


async def _topic(ticket_id="CALL1", topic_id=555):
    import bot.db as db
    await db.init_db()
    await db.upsert_topic(
        ticket_id, topic_id, unique_id="U1", company_name="Компания",
        ticket_name="Тема", priority="Стандарт", status="open",
        owner_id="1", owner_name="Оператор",
        chat_id=config_module.config.group_chat_id,
    )
    return ticket_id


async def test_append_call_notes_accumulates():
    import bot.db as db
    ticket_id = await _topic()
    await db.append_call_notes(ticket_id, ["терминал от Сбера"])
    await db.append_call_notes(ticket_id, ["ФН заполнен на 98%"])
    topic = await db.get_topic(ticket_id)
    assert "Сбера" in topic.call_notes and "98%" in topic.call_notes


async def test_append_call_notes_ignores_empty():
    import bot.db as db
    ticket_id = await _topic()
    await db.append_call_notes(ticket_id, [])
    await db.append_call_notes(ticket_id, ["   "])
    topic = await db.get_topic(ticket_id)
    assert topic.call_notes == ""


async def test_call_notes_survive_unknown_ticket():
    import bot.db as db
    await db.init_db()
    await db.append_call_notes("НЕТ-ТАКОГО", ["что-то"])   # не должно падать


# --- голосовая заметка оператора -------------------------------------------


async def test_voice_note_lands_in_call_notes_only(monkeypatch):
    """Заметка оператора после звонка — контекст ОДНОГО тикета. В общий
    retrieval она не идёт: там она всплывёт в чужом обращении как факт."""
    import bot.db as db
    from bot.transcription import process_call_note

    ticket_id = await _topic("CALL2", 556)
    indexed = []

    async def _index(source, content, **kwargs):
        indexed.append((source, content))
        return 1

    async def _transcribe(content, mime_type):
        return "клиент говорит, терминал выдал ошибку 4134, звонили в Сбер"

    async def _distill(text, ticket_title=""):
        return "терминал Сбера, ошибка 4134, обращались в банк"

    monkeypatch.setattr(config_module.config, "agent_call_fixation_enabled", True)
    note = await process_call_note(
        b"audio", "audio/ogg", ticket_id=ticket_id, ticket_title="Тема",
        _transcribe_fn=_transcribe, _distill_fn=_distill, _index_fn=_index,
    )
    assert note is not None and "4134" in note
    assert indexed == []                              # в базу знаний не ушло
    topic = await db.get_topic(ticket_id)
    assert "4134" in topic.call_notes


async def test_voice_note_skipped_when_flag_off(monkeypatch):
    from bot.transcription import process_call_note

    ticket_id = await _topic("CALL3", 557)
    monkeypatch.setattr(config_module.config, "agent_call_fixation_enabled", False)

    called = {"v": False}

    async def _transcribe(content, mime_type):
        called["v"] = True
        return "текст"

    assert await process_call_note(
        b"a", "audio/ogg", ticket_id=ticket_id, _transcribe_fn=_transcribe,
    ) is None
    assert called["v"] is False


async def test_voice_note_none_on_transcription_failure(monkeypatch):
    from bot.transcription import process_call_note

    ticket_id = await _topic("CALL4", 558)
    monkeypatch.setattr(config_module.config, "agent_call_fixation_enabled", True)

    async def _transcribe(content, mime_type):
        return None

    assert await process_call_note(
        b"a", "audio/ogg", ticket_id=ticket_id, _transcribe_fn=_transcribe,
    ) is None


async def test_call_recording_also_lands_in_call_notes(monkeypatch):
    """Полная запись звонка остаётся в базе знаний (канал работает), но теперь
    ещё и привязывается к своему тикету."""
    import bot.db as db
    from bot.transcription import process_call_recording

    ticket_id = await _topic("CALL5", 559)

    async def _transcribe(content, mime_type):
        return "длинный разговор про кассу"

    async def _distill(text, ticket_title=""):
        return "касса не печатает, заменили рулон"

    async def _index(source, content, **kwargs):
        return 42

    item_id = await process_call_recording(
        b"audio", "audio/wav", ticket_id=ticket_id, ticket_title="Тема",
        _transcribe_fn=_transcribe, _distill_fn=_distill, _index_fn=_index,
    )
    assert item_id == 42
    topic = await db.get_topic(ticket_id)
    assert "рулон" in topic.call_notes


# --- контекст агента видит заметку -----------------------------------------


async def test_agent_context_picks_up_call_notes():
    import bot.db as db
    import numpy as np
    from bot.agent.context import build_agent_context

    ticket_id = await _topic("CALL6", 560)
    await db.append_call_notes(ticket_id, ["терминал Сбера, ошибка 4134"])

    async def _embed(text, task_type="query"):
        return np.ones(4, dtype=np.float32)

    async def _similar(emb, **kw):
        return []

    async def _wiki(t):
        return None

    async def _pattern(e, k):
        return None

    ctx = await build_agent_context(
        [SimpleNamespace(user_id=1, text="что делать?", post_id=1)],
        SimpleNamespace(client_id=1), "Тема", ticket_id=ticket_id,
        _history_fn=lambda p, i: "H", _embed_fn=_embed, _similar_fn=_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=_wiki, _pattern_fn=_pattern,
    )
    assert "4134" in ctx["call_notes"]

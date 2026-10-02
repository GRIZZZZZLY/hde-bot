# tests/test_transcription.py
"""Call recording pipeline: Deepgram transcription -> Groq summary -> knowledge index."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import transcription
from bot.config import config as _config


def _mock_post_session(status: int, json_body: dict) -> MagicMock:
    """session.post(...) -> async CM with .status/.json()/.text()."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)
    resp.text = AsyncMock(return_value="err")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    sess = MagicMock()
    sess.post = MagicMock(return_value=cm)
    return sess


# --- transcribe_audio ---

@pytest.mark.asyncio
async def test_transcribe_audio_returns_transcript(monkeypatch):
    monkeypatch.setattr(_config, "deepgram_api_key", "dg_key")
    sess = _mock_post_session(200, {
        "results": {"channels": [{"alternatives": [{"transcript": "клиент просит перепрошить кассу"}]}]}
    })

    text = await transcription.transcribe_audio(b"wav-bytes", "audio/wav", sess)

    assert text == "клиент просит перепрошить кассу"
    url = sess.post.call_args.args[0]
    kwargs = sess.post.call_args.kwargs
    assert "deepgram.com" in url
    assert kwargs["headers"]["Authorization"] == "Token dg_key"
    assert kwargs["headers"]["Content-Type"] == "audio/wav"
    assert kwargs["params"]["language"] == "ru"
    assert kwargs["data"] == b"wav-bytes"


@pytest.mark.asyncio
async def test_transcribe_audio_without_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(_config, "deepgram_api_key", "")
    sess = MagicMock()

    assert await transcription.transcribe_audio(b"x", "audio/mpeg", sess) is None
    sess.post.assert_not_called()


@pytest.mark.asyncio
async def test_transcribe_audio_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(_config, "deepgram_api_key", "dg_key")
    sess = _mock_post_session(500, {})

    assert await transcription.transcribe_audio(b"x", "audio/mpeg", sess) is None


@pytest.mark.asyncio
async def test_transcribe_audio_empty_transcript_returns_none(monkeypatch):
    monkeypatch.setattr(_config, "deepgram_api_key", "dg_key")
    sess = _mock_post_session(200, {
        "results": {"channels": [{"alternatives": [{"transcript": "   "}]}]}
    })

    assert await transcription.transcribe_audio(b"x", "audio/mpeg", sess) is None


# --- summarize_transcript ---

@pytest.mark.asyncio
async def test_summarize_transcript_returns_groq_text(monkeypatch):
    monkeypatch.setattr(_config, "groq_api_key", "gq_key")
    sess = _mock_post_session(200, {
        "choices": [{"message": {"content": "Проблема: касса не печатает.\nРешение: перепрошивка."}}]
    })

    out = await transcription.summarize_transcript(
        "ну вот касса значит не печатает ничего", "Касса Эвотор", sess
    )

    assert out == "Проблема: касса не печатает.\nРешение: перепрошивка."
    payload = sess.post.call_args.kwargs["json"]
    # transcript and ticket title must reach the LLM
    joined = str(payload["messages"])
    assert "не печатает" in joined
    assert "Касса Эвотор" in joined


@pytest.mark.asyncio
async def test_summarize_transcript_without_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(_config, "groq_api_key", "")
    sess = MagicMock()

    assert await transcription.summarize_transcript("текст", "тикет", sess) is None
    sess.post.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_transcript_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(_config, "groq_api_key", "gq_key")
    sess = _mock_post_session(429, {})

    assert await transcription.summarize_transcript("текст", "тикет", sess) is None


# --- process_call_recording ---

@pytest.mark.asyncio
async def test_process_call_recording_indexes_summary(monkeypatch):
    monkeypatch.setattr(
        transcription, "transcribe_audio", AsyncMock(return_value="сырой транскрипт")
    )
    monkeypatch.setattr(
        transcription, "summarize_transcript",
        AsyncMock(return_value="Проблема: X. Решение: Y."),
    )
    index_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(transcription, "index_knowledge_item", index_mock)

    item_id = await transcription.process_call_recording(
        b"wav", "audio/wav", ticket_id="123", ticket_title="Касса"
    )

    assert item_id == 42
    kwargs = index_mock.call_args.kwargs
    assert index_mock.call_args.args[0] == "transcription" or kwargs.get("source") == "transcription"
    all_args = {**kwargs}
    assert all_args.get("ticket_id") == "123"
    # indexed content is the LLM summary, not the raw transcript
    content = index_mock.call_args.args[1] if len(index_mock.call_args.args) > 1 else all_args["content"]
    assert "Проблема: X" in content
    assert "сырой транскрипт" not in content


@pytest.mark.asyncio
async def test_process_call_recording_transcription_failed(monkeypatch):
    monkeypatch.setattr(transcription, "transcribe_audio", AsyncMock(return_value=None))
    index_mock = AsyncMock()
    monkeypatch.setattr(transcription, "index_knowledge_item", index_mock)

    item_id = await transcription.process_call_recording(
        b"wav", "audio/wav", ticket_id="123", ticket_title="Касса"
    )

    assert item_id is None
    index_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_call_recording_summary_failed_does_not_index(monkeypatch):
    monkeypatch.setattr(
        transcription, "transcribe_audio", AsyncMock(return_value="транскрипт")
    )
    monkeypatch.setattr(transcription, "summarize_transcript", AsyncMock(return_value=None))
    index_mock = AsyncMock()
    monkeypatch.setattr(transcription, "index_knowledge_item", index_mock)

    item_id = await transcription.process_call_recording(
        b"wav", "audio/wav", ticket_id="123", ticket_title="Касса"
    )

    assert item_id is None
    index_mock.assert_not_called()


# --- extract_audio_meta (message -> file_id/mime detection) ---

def _msg(audio=None, document=None, voice=None):
    m = MagicMock()
    m.audio = audio
    m.document = document
    m.voice = voice
    return m


def test_extract_audio_meta_from_audio_message():
    audio = MagicMock()
    audio.file_id = "fid1"
    audio.mime_type = "audio/mpeg"
    audio.file_name = "call.mp3"

    meta = transcription.extract_audio_meta(_msg(audio=audio))

    assert meta == ("fid1", "audio/mpeg")


def test_extract_audio_meta_from_wav_document():
    doc = MagicMock()
    doc.file_id = "fid2"
    doc.mime_type = "application/octet-stream"
    doc.file_name = "запись_звонка.wav"

    meta = transcription.extract_audio_meta(_msg(document=doc))

    assert meta == ("fid2", "audio/wav")


def test_extract_audio_meta_ignores_non_audio_document():
    doc = MagicMock()
    doc.file_id = "fid3"
    doc.mime_type = "application/pdf"
    doc.file_name = "invoice.pdf"

    assert transcription.extract_audio_meta(_msg(document=doc)) is None


def test_extract_audio_meta_ignores_voice_notes():
    voice = MagicMock()
    voice.file_id = "fid4"

    assert transcription.extract_audio_meta(_msg(voice=voice)) is None


def test_extract_audio_meta_plain_text_message():
    assert transcription.extract_audio_meta(_msg()) is None


# --- topic handler ---

def _topic_message(thread_id=555, user_id=10, is_bot=False):
    audio = MagicMock()
    audio.file_id = "fid-call"
    audio.mime_type = "audio/wav"
    audio.file_name = "call.wav"
    m = MagicMock()
    m.message_thread_id = thread_id
    m.audio = audio
    m.document = None
    m.voice = None
    m.text = None
    m.from_user = MagicMock()
    m.from_user.id = user_id
    m.from_user.is_bot = is_bot
    status = MagicMock()
    status.edit_text = AsyncMock()
    m.reply = AsyncMock(return_value=status)

    async def download(file_id, destination=None):
        destination.write(b"call-bytes")
    m.bot = MagicMock()
    m.bot.download = AsyncMock(side_effect=download)
    return m, status


def _topic_record(ticket_id="987", ticket_name="Не печатает чек"):
    rec = MagicMock()
    rec.ticket_id = ticket_id
    rec.ticket_name = ticket_name
    rec.is_deleted = False
    rec.chat_id = _config.group_chat_id
    return rec


@pytest.mark.asyncio
async def test_topic_call_recording_indexed_and_confirmed(monkeypatch):
    from bot.handlers import media_commands as cmd

    msg, status = _topic_message()
    monkeypatch.setattr(_config, "is_operator_allowed", lambda uid: True)
    monkeypatch.setattr(cmd.db, "get_topic_by_topic_id", AsyncMock(return_value=_topic_record()))
    monkeypatch.setattr(cmd, "cache_incoming_topic_media", AsyncMock())
    process_mock = AsyncMock(return_value=7)
    monkeypatch.setattr(cmd, "process_call_recording", process_mock)

    await cmd.handle_topic_call_recording(msg)

    kwargs = process_mock.call_args.kwargs
    assert process_mock.call_args.args[0] == b"call-bytes"
    assert kwargs["ticket_id"] == "987"
    assert kwargs["ticket_title"] == "Не печатает чек"
    edited = status.edit_text.call_args.args[0]
    assert "добавлен в базу знаний" in edited


@pytest.mark.asyncio
async def test_topic_call_recording_failure_reports_error(monkeypatch):
    from bot.handlers import media_commands as cmd

    msg, status = _topic_message()
    monkeypatch.setattr(_config, "is_operator_allowed", lambda uid: True)
    monkeypatch.setattr(cmd.db, "get_topic_by_topic_id", AsyncMock(return_value=_topic_record()))
    monkeypatch.setattr(cmd, "cache_incoming_topic_media", AsyncMock())
    monkeypatch.setattr(cmd, "process_call_recording", AsyncMock(return_value=None))

    await cmd.handle_topic_call_recording(msg)

    edited = status.edit_text.call_args.args[0]
    assert "Не удалось" in edited


@pytest.mark.asyncio
async def test_topic_call_recording_non_operator_ignored(monkeypatch):
    from bot.handlers import media_commands as cmd

    msg, status = _topic_message()
    monkeypatch.setattr(_config, "is_operator_allowed", lambda uid: False)
    cache_mock = AsyncMock()
    monkeypatch.setattr(cmd, "cache_incoming_topic_media", cache_mock)
    process_mock = AsyncMock()
    monkeypatch.setattr(cmd, "process_call_recording", process_mock)

    await cmd.handle_topic_call_recording(msg)

    process_mock.assert_not_called()
    cache_mock.assert_awaited()  # media still cached for reply attachments
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_topic_call_recording_no_linked_ticket_ignored(monkeypatch):
    from bot.handlers import media_commands as cmd

    msg, status = _topic_message()
    monkeypatch.setattr(_config, "is_operator_allowed", lambda uid: True)
    monkeypatch.setattr(cmd.db, "get_topic_by_topic_id", AsyncMock(return_value=None))
    monkeypatch.setattr(cmd, "cache_incoming_topic_media", AsyncMock())
    process_mock = AsyncMock()
    monkeypatch.setattr(cmd, "process_call_recording", process_mock)

    await cmd.handle_topic_call_recording(msg)

    process_mock.assert_not_called()
    msg.reply.assert_not_called()

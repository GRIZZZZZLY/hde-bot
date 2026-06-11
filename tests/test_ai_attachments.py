# tests/test_ai_attachments.py
"""Concurrent attachment download in ai_summary: order, limits, fault isolation."""
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.ai_summary import _collect_image_parts, _transcribe_audio_posts, _MAX_IMAGES
from bot.config import config as _config
from bot.hde_api import HDEPost


def _post(files: list[dict]) -> HDEPost:
    return HDEPost(post_id=1, user_id=1, text="", date_created="", files=files)


def _img(n: int, data_type: str = "png") -> dict:
    return {"name": f"img{n}.{data_type}", "url": f"https://hde/f/{n}", "data_type": data_type}


def _mock_session(responses: dict[str, tuple[int, bytes]]) -> MagicMock:
    """session.get(url) -> async CM with .status/.read() per the responses map."""
    def get(url, **kwargs):
        status, body = responses[url]
        resp = MagicMock()
        resp.status = status
        resp.read = AsyncMock(return_value=body)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm
    sess = MagicMock()
    sess.get = MagicMock(side_effect=get)
    return sess


@pytest.mark.asyncio
async def test_collect_images_keeps_order_and_caps_at_max():
    files = [_img(n) for n in range(5)]
    sess = _mock_session({f"https://hde/f/{n}": (200, f"body{n}".encode()) for n in range(5)})

    parts = await _collect_image_parts([_post(files)], sess)

    assert len(parts) == _MAX_IMAGES
    decoded = [base64.b64decode(p["inlineData"]["data"]) for p in parts]
    assert decoded == [b"body0", b"body1", b"body2"]  # conversation order


@pytest.mark.asyncio
async def test_collect_images_one_failure_does_not_block_others():
    files = [_img(n) for n in range(4)]
    big = b"x" * (4 * 1024 * 1024 + 1)  # over _MAX_IMAGE_BYTES
    sess = _mock_session({
        "https://hde/f/0": (500, b""),       # HTTP error
        "https://hde/f/1": (200, b"ok1"),
        "https://hde/f/2": (200, big),       # too large -> skipped
        "https://hde/f/3": (200, b"ok3"),
    })

    parts = await _collect_image_parts([_post(files)], sess)

    decoded = [base64.b64decode(p["inlineData"]["data"]) for p in parts]
    assert decoded == [b"ok1", b"ok3"]


@pytest.mark.asyncio
async def test_collect_images_ignores_non_image_types():
    files = [
        {"name": "doc.pdf", "url": "https://hde/f/doc", "data_type": "pdf"},
        _img(1, "jpg"),
    ]
    sess = _mock_session({"https://hde/f/1": (200, b"pic")})

    parts = await _collect_image_parts([_post(files)], sess)

    assert len(parts) == 1
    sess.get.assert_called_once()  # the pdf was never downloaded


@pytest.mark.asyncio
async def test_transcribe_audio_keeps_order_and_skips_failures(monkeypatch):
    monkeypatch.setattr(_config, "deepgram_api_key", "test_key")
    files = [
        {"name": "a0.mp3", "url": "https://hde/a/0", "data_type": "mp3"},
        {"name": "a1.mp3", "url": "https://hde/a/1", "data_type": "mp3"},
        {"name": "a2.mp3", "url": "https://hde/a/2", "data_type": "mp3"},
    ]
    sess = _mock_session({
        "https://hde/a/0": (200, b"audio0"),
        "https://hde/a/1": (500, b""),  # download fails -> no transcript
        "https://hde/a/2": (200, b"audio2"),
    })

    def post(url, data=b"", **kwargs):
        text = "первый звонок" if data == b"audio0" else "второй звонок"
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "results": {"channels": [{"alternatives": [{"transcript": text}]}]}
        })
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm
    sess.post = MagicMock(side_effect=post)

    transcripts = await _transcribe_audio_posts([_post(files)], sess)

    assert transcripts == ["первый звонок", "второй звонок"]

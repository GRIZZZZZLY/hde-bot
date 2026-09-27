import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import bot.hde_api as hde_api_module
from bot.formatter import (
    format_assignment_message,
    format_client_reply,
    format_message_deleted,
    format_message_edited,
    format_morning_digest,
    format_pre_sla_alert_topic,
    format_refresh_result,
    format_unassigned_message,
    make_topic_name,
    priority_emoji,
)
from bot.hde_api import HDEApiClient
from bot.hde_webhook import _build_event_key, _normalize_payload


def test_priority_emoji_defaults():
    assert priority_emoji("critical") == "🔴"
    assert priority_emoji("unknown") == "🟡"


def test_make_topic_name_truncation():
    name = make_topic_name("ABC-123", "Company", "X" * 200, "high")
    assert len(name) <= 128
    assert name.startswith("🟠 ")


def test_format_assignment_message_contains_key_fields():
    text = format_assignment_message(
        display_id="ABC-123",
        company_name="ACME",
        ticket_name="Broken printer",
        status="open",
        priority="high",
        link="https://hde.example.com/tickets/1",
    )
    assert "Тикет назначен на вас" in text
    assert "Broken printer" in text
    assert "https://hde.example.com/tickets/1" in text


def test_format_client_reply_contains_message_and_link():
    text = format_client_reply(
        user_name="Alice",
        message="Please help",
        sla_remaining="30",
        link="https://hde.example.com/tickets/1",
    )
    assert "Alice" in text
    assert "Please help" in text
    assert "https://hde.example.com/tickets/1" in text


def test_format_unassigned_message_mentions_delayed_delete():
    text = format_unassigned_message("ABC-123", "https://hde.example.com/tickets/1")
    assert "Тикет снят с вас" in text
    assert "8 часов" in text


def test_format_pre_sla_alert_mentions_minutes():
    text = format_pre_sla_alert_topic(
        minutes_left=10,
        ticket_name="Broken printer",
        link="https://hde.example.com/tickets/1",
    )
    assert "10 мин" in text
    assert "Broken printer" in text


def test_normalize_payload_maps_hde_fields():
    payload = _normalize_payload(
        {
            "event_type": "client_reply",
            "ticket_id": "TKT-1",
            "unique_id": "ABC-123",
            "ticket_name": "Broken printer",
            "company_name": "ACME",
            "priority": "high",
            "status": "open",
            "owner_id": "me",
            "owner_name": "Me",
            "answer_last_without_html": "Please help",
            "link_staff": "https://hde.example.com/tickets/1",
            "sla_remaining_minutes": "30",
            "attachments_preview_links": '<a href="https://files.example.com/a.jpg">a.jpg</a>',
        }
    )
    assert payload["message"] == "Please help"
    assert payload["link"] == "https://hde.example.com/tickets/1"
    assert payload["unique_id"] == "ABC-123"
    assert len(payload["attachments"]) == 1
    assert payload["attachments"][0].url == "https://files.example.com/a.jpg"


def test_normalize_payload_accepts_assigned_on_create():
    payload = _normalize_payload(
        {
            "event_type": "assigned_on_create",
            "ticket_id": "TKT-2",
            "unique_id": "ABC-456",
            "ticket_name": "New issue",
        }
    )
    assert payload["event_type"] == "assigned_on_create"
    assert payload["ticket_id"] == "TKT-2"
    assert payload["unique_id"] == "ABC-456"


def test_event_key_is_stable_for_same_payload():
    payload = _normalize_payload(
        {
            "event_type": "ticket_updated",
            "ticket_id": "TKT-1",
            "ticket_name": "Broken printer",
        }
    )
    assert _build_event_key(payload) == _build_event_key(payload)


def test_format_message_edited_contains_id():
    text = format_message_edited("ABC-123")
    assert "обновлено" in text
    assert "ABC-123" in text


def test_format_message_deleted_contains_id():
    text = format_message_deleted("ABC-123")
    assert "удалено" in text
    assert "ABC-123" in text


def test_format_morning_digest_only_unassigned_equipment():
    text = format_morning_digest(unassigned_equipment_count=2)
    assert "Сводка за ночь" in text
    assert "Неприсвоенных (Оборудование):" in text
    assert "<b>2</b>" in text
    assert "Назначено за ночь" not in text
    assert "Открытых тикетов сейчас" not in text


def test_format_refresh_result_no_changes():
    text = format_refresh_result(active_count=3, hde_count=3, marked_deleted=[])
    assert "Синхронизация завершена" in text
    assert "Активных топиков:" in text
    assert "<b>3</b>" in text
    assert "расхождений нет" in text


def test_normalize_payload_extracts_department():
    payload = _normalize_payload({
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-5",
        "department": "Оборудование",
    })
    assert payload["department"] == "Оборудование"


def test_normalize_payload_department_defaults_to_empty():
    payload = _normalize_payload({
        "event_type": "assigned_on_create",
        "ticket_id": "TKT-6",
    })
    assert payload["department"] == ""


@pytest.mark.asyncio
async def test_shared_connector_reused_and_survives_session_close():
    conn = hde_api_module._get_connector()
    # Same pool handed back on repeat calls within one event loop.
    assert hde_api_module._get_connector() is conn
    # A borrowing session (connector_owner=False) must NOT close the shared pool.
    async with aiohttp.ClientSession(connector=conn, connector_owner=False):
        pass
    assert not conn.closed
    # Explicit shutdown closes it and clears the module state.
    await hde_api_module.close_shared_connector()
    assert conn.closed
    assert hde_api_module._shared_connector is None


@pytest.mark.asyncio
async def test_paginated_hde_fetch_borrows_one_shared_connector(monkeypatch):
    connector_ids: list[int] = []
    session_count = 0

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, page: int):
            self.page = page

        async def json(self):
            return {
                "data": {
                    str(self.page): {
                        "id": self.page,
                        "unique_id": f"ABC-{self.page:03d}",
                        "title": f"Ticket {self.page}",
                        "owner_id": "me",
                    }
                },
                "meta": {"total_pages": 3},
            }

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            nonlocal session_count
            session_count += 1
            assert kwargs["connector_owner"] is False
            connector_ids.append(id(kwargs["connector"]))

        def get(self, url, params=None):
            return FakeResponse(int(params["page"]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    client = HDEApiClient()
    tickets = await client.get_my_open_tickets()

    assert [ticket.ticket_id for ticket in tickets] == ["1", "2", "3"]
    assert session_count == 3
    assert len(set(connector_ids)) == 1


_TICKET_INFO_JSON = {
    "data": {
        "user_id": 7,
        "user_name": "Alice",
        "user_lastname": "Smith",
        "owner_id": 2,
        "owner_name": "Bob",
        "owner_lastname": "Jones",
    }
}


@pytest.mark.asyncio
async def test_sessions_use_request_timeout(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        async def json(self):
            return _TICKET_INFO_JSON

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get(self, url, params=None):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    client = HDEApiClient()
    await client.get_ticket_info("42")

    timeout = captured["timeout"]
    assert timeout.total == 30
    assert timeout.connect == 10


@pytest.mark.asyncio
async def test_shared_connector_caches_dns():
    conn = hde_api_module._get_connector()
    assert conn._cached_hosts._ttl == 300
    await hde_api_module.close_shared_connector()


@pytest.mark.asyncio
async def test_get_retries_once_on_stale_keepalive_connection(monkeypatch):
    attempts = {"n": 0}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        async def json(self):
            return _TICKET_INFO_JSON

        async def text(self):
            return ""

        async def __aenter__(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise aiohttp.ServerDisconnectedError()
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def get(self, url, params=None):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    client = HDEApiClient()
    info = await client.get_ticket_info("42")

    assert info.client_id == 7
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_get_raises_after_last_disconnect(monkeypatch):
    attempts = {"n": 0}

    class FakeResponse:
        async def __aenter__(self):
            attempts["n"] += 1
            raise aiohttp.ServerDisconnectedError()

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def get(self, url, params=None):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    async def fake_sleep(delay):
        pass

    monkeypatch.setattr("bot.hde_api.asyncio.sleep", fake_sleep)

    client = HDEApiClient()
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await client.get_ticket_info("42")

    assert attempts["n"] == HDEApiClient._GET_ATTEMPTS


@pytest.mark.asyncio
async def test_post_not_retried_on_server_disconnect(monkeypatch):
    attempts = {"n": 0}

    class FakeResponse:
        async def __aenter__(self):
            attempts["n"] += 1
            raise aiohttp.ServerDisconnectedError()

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def post(self, url, data=None):
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    client = HDEApiClient()
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await client.add_post("42", text="hi")

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_open_tickets_pages_fetched_concurrently(monkeypatch):
    active = {"now": 0, "max": 0}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, page: int):
            self.page = page

        async def json(self):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.02)
            active["now"] -= 1
            return {
                "data": {
                    str(self.page): {
                        "id": self.page,
                        "unique_id": f"ABC-{self.page:03d}",
                        "title": f"Ticket {self.page}",
                        "owner_id": "me",
                    }
                },
                "meta": {"total_pages": 4},
            }

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        def get(self, url, params=None):
            return FakeResponse(int(params["page"]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.hde_api.aiohttp.ClientSession", FakeSession)

    client = HDEApiClient()
    tickets = await client.get_my_open_tickets()

    assert [t.ticket_id for t in tickets] == ["1", "2", "3", "4"]
    # Page 1 is fetched alone (it carries total_pages); pages 2-4 must overlap.
    assert active["max"] >= 2


@pytest.mark.asyncio
async def test_get_ticket_open_status_closed(monkeypatch):
    async def fake_read_response(self, response):
        return {"data": {"status": "closed", "link_staff": "https://hde.example.com/t/1"}}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("123")

    assert result == (True, "https://hde.example.com/t/1")


@pytest.mark.asyncio
async def test_get_ticket_open_status_open(monkeypatch):
    async def fake_read_response(self, response):
        return {"data": {"status": "open", "link_staff": "https://hde.example.com/t/2"}}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("456")

    assert result == (False, "https://hde.example.com/t/2")


@pytest.mark.asyncio
async def test_get_ticket_open_status_api_error(monkeypatch):
    async def fake_read_response(self, response):
        raise RuntimeError("network error")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("bot.hde_api.HDEApiClient._read_response", fake_read_response)

    with patch("aiohttp.ClientSession", return_value=mock_sess):
        client = HDEApiClient()
        result = await client.get_ticket_open_status("789")

    assert result is None


def test_format_refresh_result_deleted_shows_link():
    text = format_refresh_result(
        active_count=2,
        hde_count=3,
        deleted=[("Сломан принтер", "42", "https://hde.example.com/t/42")],
    )
    assert "Удалено устаревших" in text
    assert "Сломан принтер" in text
    assert "https://hde.example.com/t/42" in text
    assert "Открыть в HDE" in text
    assert "company_name" not in text


def test_format_refresh_result_deleted_no_link():
    text = format_refresh_result(
        active_count=2,
        hde_count=3,
        deleted=[("Тикет без ссылки", "99", "")],
    )
    assert "Тикет без ссылки" in text
    assert "Открыть в HDE" not in text


# --- клиентский rate-limiter (инцидент бана HDE 2026-07-13) ---
from bot.hde_api import _RateLimiter


async def test_rate_limiter_spaces_requests():
    clock = [0.0]
    slept = []

    def _now():
        return clock[0]

    async def _sleep(d):
        slept.append(d)
        clock[0] += d

    rl = _RateLimiter(120, _time_fn=_now, _sleep_fn=_sleep)  # 0.5s между запросами
    await rl.acquire()   # первый — без ожидания
    await rl.acquire()   # ждёт 0.5
    await rl.acquire()   # ждёт 0.5
    assert slept == [0.5, 0.5]


async def test_rate_limiter_disabled_when_zero():
    slept = []

    async def _sleep(d):
        slept.append(d)

    rl = _RateLimiter(0, _time_fn=lambda: 0.0, _sleep_fn=_sleep)
    await rl.acquire()
    await rl.acquire()
    assert slept == []            # rpm<=0 → троттл выключен, без задержек
    assert rl.min_interval == 0.0


def test_post_sort_key_orders_by_date_then_time():
    """Final review F7: "HH:MM:SS DD.MM.YYYY" строкой сортировался по времени суток."""
    from bot.hde_api import HDEPost, post_sort_key

    late_day1 = HDEPost(post_id=1, user_id=1, text="", date_created="18:00:00 13.09.2026")
    early_day2 = HDEPost(post_id=2, user_id=1, text="", date_created="09:00:00 14.09.2026")
    assert [p.post_id for p in sorted([early_day2, late_day1], key=post_sort_key)] == [1, 2]


def test_post_sort_key_falls_back_to_post_id_on_bad_date():
    from bot.hde_api import HDEPost, post_sort_key

    posts = [HDEPost(post_id=7, user_id=1, text="", date_created="мусор"),
             HDEPost(post_id=3, user_id=1, text="", date_created="")]
    assert [p.post_id for p in sorted(posts, key=post_sort_key)] == [3, 7]

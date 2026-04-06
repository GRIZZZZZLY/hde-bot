from bot.formatter import (
    format_assignment_message,
    format_client_reply,
    format_message_deleted,
    format_message_edited,
    format_morning_digest,
    format_pre_sla_alert,
    format_refresh_result,
    format_unassigned_message,
    make_topic_name,
    priority_emoji,
)
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
    assert "Ответ клиента" in text
    assert "Alice" in text
    assert "Please help" in text
    assert "https://hde.example.com/tickets/1" in text


def test_format_unassigned_message_mentions_delayed_delete():
    text = format_unassigned_message("ABC-123", "https://hde.example.com/tickets/1")
    assert "Тикет снят с вас" in text
    assert "8 часов" in text


def test_format_pre_sla_alert_mentions_minutes():
    text = format_pre_sla_alert(
        display_id="ABC-123",
        ticket_name="Broken printer",
        company_name="ACME",
        minutes_left=10,
        link="https://hde.example.com/tickets/1",
    )
    assert "10 минут" in text
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


def test_format_morning_digest_empty_night():
    text = format_morning_digest(
        night_start_label="18:00 03.04",
        night_end_label="08:00 04.04",
        assigned_tickets=[],
        total_open=5,
        open_tickets_with_sla=[],
    )
    assert "Сводка за ночь" in text
    assert "Назначено за ночь:" in text
    assert "<b>0</b>" in text
    assert "Открытых тикетов сейчас:" in text
    assert "<b>5</b>" in text


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

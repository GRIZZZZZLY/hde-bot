"""Tests for the golden-set subsystem (Phase 0B)."""
import json

import pytest

from bot.optimizer.golden import (
    freeze_golden,
    golden_ticket_ids,
    load_golden,
)


def _case(case_id="g001", ticket_id="T1", expected_action="ANSWER"):
    return {
        "case_id": case_id,
        "ticket_id": ticket_id,
        "title": "Не печатает чек",
        "history": "Клиент: касса не печатает чек",
        "client_text": "касса не печатает чек",
        "expected_action": expected_action,
        "reference_answer": "Проверьте бумагу и перезапустите кассу.",
        "rubric": "",
        "type_id": "5",
    }


def test_freeze_produces_hash_and_load_roundtrip(tmp_path):
    golden = freeze_golden([_case()], version="v1", frozen_at="2026-07-10T00:00:00Z")
    assert golden["version"] == "v1"
    assert golden["content_hash"]
    p = tmp_path / "golden_v1.json"
    p.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
    loaded = load_golden(str(p))
    assert loaded["cases"][0]["case_id"] == "g001"


def test_load_rejects_tampered_cases(tmp_path):
    golden = freeze_golden([_case()], version="v1", frozen_at="2026-07-10T00:00:00Z")
    golden["cases"][0]["reference_answer"] = "ПОДМЕНЁННЫЙ ЭТАЛОН"
    p = tmp_path / "golden_v1.json"
    p.write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden(str(p))


def test_freeze_validates_cases():
    bad = _case(expected_action="MAYBE")
    with pytest.raises(ValueError):
        freeze_golden([bad], version="v1")
    incomplete = _case()
    del incomplete["reference_answer"]
    with pytest.raises(ValueError):
        freeze_golden([incomplete], version="v1")


def test_golden_ticket_ids():
    golden = freeze_golden(
        [_case("g001", "T1"), _case("g002", "T2")],
        version="v1", frozen_at="2026-07-10T00:00:00Z",
    )
    assert golden_ticket_ids(golden) == {"T1", "T2"}

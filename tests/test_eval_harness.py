"""Tests for eval harness pure parts (no network)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_prompt import EvalCache, _prompt_hash, load_samples


def test_prompt_hash_stable_and_distinct():
    assert _prompt_hash("a") == _prompt_hash("a")
    assert _prompt_hash("a") != _prompt_hash("b")


def test_eval_cache_roundtrip(tmp_path):
    cache = EvalCache(str(tmp_path / "c.db"))
    assert cache.get("k1") is None
    cache.put("k1", "ответ")
    assert cache.get("k1") == "ответ"
    # повторное открытие видит данные
    cache2 = EvalCache(str(tmp_path / "c.db"))
    assert cache2.get("k1") == "ответ"


def test_load_samples(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE optimization_samples ("
        "id INTEGER PRIMARY KEY, ticket_id TEXT, title TEXT, history TEXT, "
        "ai_answer TEXT, op_answer TEXT, outcome TEXT, confidence INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO optimization_samples "
        "(ticket_id, title, history, ai_answer, op_answer, outcome) "
        "VALUES ('1', 't', 'h', 'a', 'op', 'corrected')"
    )
    conn.commit()
    rows, source = load_samples(str(db), days=365)
    assert len(rows) == 1
    assert rows[0]["op_answer"] == "op"
    assert "кнопки" in source  # дамп без ai_suggestions → резервный источник


def test_load_samples_prefers_reconcile_source(tmp_path):
    """Есть вердикты сверки — берём их, а не кнопочную таблицу."""
    db = tmp_path / "t2.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE optimization_samples ("
        "id INTEGER PRIMARY KEY, ticket_id TEXT, title TEXT, history TEXT, "
        "ai_answer TEXT, op_answer TEXT, outcome TEXT, confidence INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO optimization_samples "
        "(ticket_id, title, history, ai_answer, op_answer, outcome) "
        "VALUES ('old', 't', 'h', 'a', 'кнопочный эталон', 'corrected')"
    )
    conn.execute(
        "CREATE TABLE ai_suggestions ("
        "id INTEGER PRIMARY KEY, ticket_id TEXT, title TEXT, history TEXT, "
        "ai_answer TEXT, judge_reference_answer TEXT, human_label TEXT, "
        "judge_label TEXT, confidence INTEGER, judge_category TEXT, "
        "prompt_version TEXT, created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO ai_suggestions "
        "(ticket_id, title, history, ai_answer, judge_reference_answer, judge_label) "
        "VALUES ('T9', 'тема', 'история', 'черновик', 'ответ оператора', 'corrected')"
    )
    conn.commit()

    rows, source = load_samples(str(db), days=365)

    assert "сверка" in source
    assert [r["ticket_id"] for r in rows] == ["T9"]
    assert rows[0]["op_answer"] == "ответ оператора"
    assert rows[0]["outcome"] == "corrected"

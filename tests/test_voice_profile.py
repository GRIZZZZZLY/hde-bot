"""Tests for voice profile extraction and loading."""
import json

from bot.voice_profile import extract_voice_examples, load_voice_examples


def test_extract_filters_by_length():
    texts = ["коротко", "х" * 700, "Перезагрузите кассу и проверьте чековую ленту, после этого пробейте тестовый чек"]
    out = extract_voice_examples(texts)
    assert len(out) == 1
    assert out[0].startswith("Перезагрузите")


def test_extract_dedupes_case_insensitive():
    t = "Перезагрузите кассу и проверьте чековую ленту, затем пробейте тестовый чек"
    out = extract_voice_examples([t, t.upper()])
    assert len(out) == 1


def test_extract_caps_at_max():
    texts = [
        f"Проверьте подключение кассы номер {i} к сети и перезапустите драйвер ККТ"
        for i in range(20)
    ]
    out = extract_voice_examples(texts, max_examples=8)
    assert len(out) == 8


def test_extract_normalizes_whitespace():
    out = extract_voice_examples(["Проверьте   подключение\n\nкассы к сети и перезапустите драйвер ККТ"])
    assert "  " not in out[0]
    assert "\n" not in out[0]


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    import bot.voice_profile as vp
    monkeypatch.setattr(vp, "_PROFILE_PATH", tmp_path / "nope.json")
    assert load_voice_examples() == []


def test_load_reads_examples(tmp_path, monkeypatch):
    import bot.voice_profile as vp
    p = tmp_path / "voice_profile.json"
    p.write_text(json.dumps({"examples": ["пример один", 42]}), encoding="utf-8")
    monkeypatch.setattr(vp, "_PROFILE_PATH", p)
    assert load_voice_examples() == ["пример один"]

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "eval_voice", Path(__file__).resolve().parents[1] / "scripts" / "eval_voice.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_pattern_flags():
    f = ev.pattern_flags("Скачайте AnyDesk и ждите звонка инженера. Получилось? Какая модель?")
    assert f == {"remote": True, "wait": True, "check_back": True, "multi_question": True}
    assert not any(ev.pattern_flags("Перезагрузите роутер.").values())


def test_summarize_rates():
    rows = [{"old": {"client": "Скачайте AnyDesk"}, "new": {"client": "Перезагрузите роутер. Получилось?"}},
            {"old": {"client": "Ждите звонка инженера"}, "new": {"client": "Смените порт."}}]
    s = ev.summarize(rows)
    assert s["old"]["remote"] == 0.5 and s["new"]["remote"] == 0.0
    assert s["old"]["wait"] == 0.5 and s["new"]["check_back"] == 0.5


def test_ab_pairs_are_blind_and_reproducible():
    rows = [{"case_id": i, "old": {"client": f"o{i}"}, "new": {"client": f"n{i}"}} for i in range(6)]
    pairs, key = ev.make_ab_pairs(rows, seed=1)
    assert all(set(p) == {"case_id", "A", "B"} for p in pairs)
    assert {key[str(p["case_id"])] for p in pairs} <= {"A=old", "A=new"}
    assert ev.make_ab_pairs(rows, seed=1) == (pairs, key)

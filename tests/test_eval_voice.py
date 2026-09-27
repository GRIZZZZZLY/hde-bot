import asyncio
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def test_summarize_excludes_failed_generations():
    """Final review F6: сбой генерации — не «чистый черновик» в статистике."""
    fail = {"failed": True, "client": "", "lint": {"hard": []}}
    rows = [{"old": {"client": "Скачайте AnyDesk"}, "new": fail},
            {"old": fail, "new": {"client": "Смените порт."}},
            {"old": {"client": "Перезагрузите роутер."}, "new": {"client": "Скачайте AnyDesk"}}]
    s = ev.summarize(rows)
    assert s["old"]["failed"] == 1 and s["old"]["n"] == 2
    assert s["new"]["failed"] == 1 and s["new"]["n"] == 2
    assert s["old"]["remote"] == 0.5 and s["new"]["remote"] == 0.5


async def test_one_marks_failed_draft_and_mirrors_pipeline_lint(monkeypatch):
    """Final review F6: None от модели → failed; lint видит demos и факты, как пайплайн."""
    import bot.agent.context as ctx_mod
    import bot.agent.generate as gen_mod
    import bot.agent.lint as lint_mod
    from bot.config import config

    monkeypatch.setattr(config, "agent_voice_v2_enabled", config.agent_voice_v2_enabled)
    ctx = {"history": "Клиент: q", "evidence": [], "grounds": [], "first_staff_reply": False,
           "demos": [{"used_excerpt": "Ответ оператора: https://posiflora.teamly.ru/abc"}],
           "ticket_facts": "Окружение: Атол", "attachments": "", "call_notes": ""}
    monkeypatch.setattr(ctx_mod, "build_agent_context", AsyncMock(return_value=ctx))
    case = {"posts": [], "info": {"client_id": 1, "client_name": "c", "owner_id": 2,
                                  "owner_name": "o"}, "title": "t", "ticket_id": "1"}

    monkeypatch.setattr(gen_mod, "generate_agent_draft", AsyncMock(return_value=None))
    failed = await ev._one(case, True)
    assert failed["failed"] is True and failed["client"] == "" and failed["action"] is None

    seen = {}
    real = lint_mod.check_draft

    def spy(client, memo, **kw):
        seen.update(kw)
        return real(client, memo, **kw)

    monkeypatch.setattr(lint_mod, "check_draft", spy)
    monkeypatch.setattr(gen_mod, "generate_agent_draft", AsyncMock(return_value={
        "action": "ANSWER", "client": "См. https://posiflora.teamly.ru/abc", "memo": "m",
        "analysis": "", "source_ids": []}))
    row = await ev._one(case, True)
    assert not row.get("failed") and row["lint"]["hard"] == []
    assert "Атол" in seen["facts"]

    monkeypatch.setattr(gen_mod, "generate_agent_draft", AsyncMock(return_value={
        "action": "ESCALATE", "client": "", "memo": "m", "source_ids": ["KB#99"]}))
    await ev._one(case, True)
    assert seen["source_ids"] == []


async def test_run_does_not_touch_knowledge_last_used(monkeypatch, tmp_path):
    """Final review F6: прогон офлайн-проверки не пишет last_used_at в прод-БД."""
    import bot.db as db_mod

    set_path = tmp_path / "set.json"
    set_path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    monkeypatch.setattr(ev, "SET_PATH", set_path)
    monkeypatch.setattr(ev, "RUN_PATH", tmp_path / "run.json")
    real = AsyncMock()
    monkeypatch.setattr(db_mod, "update_knowledge_last_used", real)
    await ev.run()
    await db_mod.update_knowledge_last_used([1, 2])
    real.assert_not_awaited()


async def test_run_new_side_in_parts_keeps_old_results(monkeypatch, tmp_path):
    """Дневной лимит Groq 200k токенов: повтор — только новой версии и частями;
    ответы старой версии из прошлого прогона не трогаем."""
    cases = [{"case_id": i, "ticket_id": str(i), "reference": f"r{i}"} for i in range(4)]
    set_path = tmp_path / "set.json"
    set_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    run_path = tmp_path / "run.json"
    old_rows = [{"case_id": i, "ticket_id": str(i), "reference": f"r{i}",
                 "old": {"client": f"old{i}"}, "new": {"failed": True, "client": ""}}
                for i in range(3)]                       # case 3 ещё не прогоняли
    run_path.write_text(json.dumps(old_rows), encoding="utf-8")
    monkeypatch.setattr(ev, "SET_PATH", set_path)
    monkeypatch.setattr(ev, "RUN_PATH", run_path)
    import bot.db as db_mod
    # run() глушит update_knowledge_last_used на весь процесс скрипта —
    # в тестах возвращаем настоящую функцию, иначе заглушка утечёт в соседние тесты
    monkeypatch.setattr(db_mod, "update_knowledge_last_used", db_mod.update_knowledge_last_used)
    calls = []

    async def fake_one(case, v2):
        calls.append((case["case_id"], v2))
        return {"client": f"new{case['case_id']}", "lint": {"hard": []}}

    monkeypatch.setattr(ev, "_one", fake_one)
    monkeypatch.setattr(ev.asyncio, "sleep", AsyncMock())

    await ev.run(side="new", start=1, count=3)

    assert calls == [(1, True), (2, True), (3, True)]      # только новая версия, кейсы 1..3
    rows = {r["case_id"]: r for r in json.loads(run_path.read_text(encoding="utf-8"))}
    assert rows[0]["new"] == {"failed": True, "client": ""}  # вне части — не тронут
    assert rows[1]["old"] == {"client": "old1"} and rows[1]["new"]["client"] == "new1"
    assert rows[3]["old"]["failed"] is True and rows[3]["new"]["client"] == "new3"
    assert [r["case_id"] for r in json.loads(run_path.read_text(encoding="utf-8"))] == [0, 1, 2, 3]


def test_ab_pairs_are_blind_and_reproducible():
    rows = [{"case_id": i, "old": {"client": f"o{i}"}, "new": {"client": f"n{i}"}} for i in range(6)]
    pairs, key = ev.make_ab_pairs(rows, seed=1)
    assert all(set(p) == {"case_id", "A", "B"} for p in pairs)
    assert {key[str(p["case_id"])] for p in pairs} <= {"A=old", "A=new"}
    assert ev.make_ab_pairs(rows, seed=1) == (pairs, key)


@pytest.mark.asyncio
async def test_build_survives_failing_ticket_and_sorts_by_date(monkeypatch, tmp_path):
    """Build skips NULL anchor, handles HDEApiError on comments, sorts posts by date_created."""
    from bot.hde_api import HDEApiError, HDEPost, HDETicketInfo

    # Create temp DB with 3 test cases
    db_file = tmp_path / "test.db"
    con = sqlite3.connect(str(db_file))
    con.execute(
        "CREATE TABLE ai_suggestions(id INTEGER PRIMARY KEY, ticket_id INTEGER, "
        "title TEXT, context_until_post_id TEXT, judge_reference_answer TEXT)"
    )
    con.execute("INSERT INTO ai_suggestions VALUES (1, 100, 'title1', '3', 'ref1')")
    con.execute("INSERT INTO ai_suggestions VALUES (2, 101, 'title2', NULL, 'ref2')")
    con.execute("INSERT INTO ai_suggestions VALUES (3, 102, 'title3', '2', 'ref3')")
    con.commit()
    con.close()

    # Fake HDEApiClient
    class FakeClient:
        async def get_ticket_info(self, tid):
            return HDETicketInfo(client_id=1, client_name="c", owner_id=2, owner_name="o")

        async def get_ticket_posts(self, tid):
            if tid == "100":
                return [
                    HDEPost(post_id="1", user_id="u1", text="t1", date_created="10:00:00 01.01.2026", is_comment=False),
                    HDEPost(post_id="3", user_id="u3", text="t3", date_created="10:00:00 03.01.2026", is_comment=False),
                ]
            elif tid == "102":
                return [
                    HDEPost(post_id="1", user_id="u1", text="t1", date_created="10:00:00 01.01.2026", is_comment=False),
                    HDEPost(post_id="2", user_id="u2", text="t2", date_created="10:00:00 02.01.2026", is_comment=False),
                ]
            return []

        async def get_ticket_comments(self, tid):
            if tid == "102":
                return [HDEPost(post_id="2", user_id="uc", text="tc", date_created="12:00:00 01.01.2026", is_comment=True)]
            raise HDEApiError("fail")

    # Monkeypatch
    monkeypatch.setattr("bot.hde_api.HDEApiClient", FakeClient)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    tmp_out = tmp_path / "out.json"
    monkeypatch.setattr(ev, "SET_PATH", tmp_out)

    # Run build
    await ev.build(50, str(db_file))

    # Verify
    output = json.loads(tmp_out.read_text(encoding="utf-8"))
    cases = output["cases"]

    # NULL anchor excluded
    assert len(cases) == 2, "NULL anchor should be excluded"

    # Case 1 (ticket 100): anchor 3, posts 1,3
    case1 = next((c for c in cases if c["ticket_id"] == "100"), None)
    assert case1 is not None
    assert len(case1["posts"]) == 2
    assert case1["posts"][0]["post_id"] == "1"
    assert case1["posts"][1]["post_id"] == "3"

    # Case 2 (ticket 102): anchor 2, posts 1,2,comment(2) sorted by date
    case2 = next((c for c in cases if c["ticket_id"] == "102"), None)
    assert case2 is not None
    assert len(case2["posts"]) == 3
    # HDE dates ("HH:MM:SS DD.MM.YYYY") sort by day, then time: post1 (01.01 10:00),
    # comment (01.01 12:00), post2 (02.01 10:00) — final review F7
    assert [(p["post_id"], p["is_comment"]) for p in case2["posts"]] == [
        ("1", False), ("2", True), ("2", False)]

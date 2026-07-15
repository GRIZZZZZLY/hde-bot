import bot.config as config_module


def test_staff_ids_and_mining_flag(monkeypatch):
    monkeypatch.setenv("AGENT_STAFF_USER_IDS", "10, 20 ,30")
    monkeypatch.setenv("AGENT_DIALOGUE_MINING_ENABLED", "true")
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ("10", "20", "30")
    assert fresh.agent_dialogue_mining_enabled is True


def test_mining_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_DIALOGUE_MINING_ENABLED", raising=False)
    assert config_module.Config.from_env().agent_dialogue_mining_enabled is False


from types import SimpleNamespace

from bot.agent.dialogue_mining import (
    _strip_html,
    is_staff_post,
    sort_posts,
    split_ticket_into_pairs,
    staff_id_set,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


def test_strip_html():
    assert _strip_html("<p>не <b>печатает</b></p>") == "не печатает"


def test_sort_posts_orders_oldest_first():
    posts = [_post(3, "c", "c"), _post(1, "a", "a"), _post(2, "b", "b")]
    assert [p.post_id for p in sort_posts(posts)] == [1, 2, 3]


def test_staff_id_set_and_detection():
    assert staff_id_set("owner1", ("10", "20")) == {"10", "20"}
    assert staff_id_set("owner1", ()) == {"owner1"}
    assert is_staff_post(_post(1, "10", "x"), {"10"}) is True
    assert is_staff_post(_post(2, "99", "x"), {"10"}) is False


def test_split_merges_consecutive_operator_posts():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не работает"),
        _post(2, "op", "Добрый день."),
        _post(3, "op", "Уточните модель кассы."),
        _post(4, "op", "И пришлите фото ошибки."),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1                              # три staff-поста = один turn
    ans = pairs[0]["operator_answer"]
    assert "Добрый день." in ans and "модель кассы" in ans and "фото ошибки" in ans
    assert pairs[0]["operator_message_id"] == "2"       # первый пост turn'а
    assert pairs[0]["source_message_id"] == "1"         # последний клиентский пост


def test_split_multi_turn_no_future_leak():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не печатает"),
        _post(2, "op", "проверьте бумагу"),
        _post(3, "client", "бумага есть"),
        _post(4, "op", "перезагрузите кассу"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 2
    assert pairs[0]["operator_answer"] == "проверьте бумагу"
    assert "перезагрузите" not in pairs[0]["context"]   # нет утечки будущего
    assert "перезагрузите" not in pairs[0]["operator_answer"]
    assert "бумага есть" in pairs[1]["context"]
    assert pairs[0]["content_hash"] != pairs[1]["content_hash"]


def test_split_reversed_input_still_correct():
    staff = {"op"}
    posts = [
        _post(4, "op", "перезагрузите кассу"),
        _post(3, "client", "бумага есть"),
        _post(2, "op", "проверьте бумагу"),
        _post(1, "client", "касса не печатает"),
    ]  # обратный порядок на входе
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert pairs[0]["operator_answer"] == "проверьте бумагу"   # сортировка сработала
    assert "перезагрузите" not in pairs[0]["context"]


def test_split_skips_comments_and_leading_staff():
    staff = {"op"}
    posts = [
        _post(1, "op", "внутренняя заметка", is_comment=True),
        _post(2, "op", "ответ без клиента"),
        _post(3, "client", "вопрос"),
        _post(4, "op", "ответ"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1
    assert pairs[0]["operator_answer"] == "ответ"


def test_content_hash_changes_with_edited_answer():
    staff = {"op"}
    base = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    edited = [_post(1, "client", "вопрос"), _post(2, "op", "ответ исправлен")]
    h1 = split_ticket_into_pairs("T1", base, staff)[0]["content_hash"]
    h2 = split_ticket_into_pairs("T1", edited, staff)[0]["content_hash"]
    assert h1 != h2                                     # правка ответа → новый хэш


import numpy as np

from bot.agent.dialogue_mining import build_embedding_text, mine_ticket_pairs, reembed_pending


class _FakeHDE:
    def __init__(self, posts, client_id="client"):
        self._posts = posts
        self._client_id = client_id

    async def get_all_ticket_posts(self, ticket_id, *, page_size=20, max_pages=25):
        return self._posts

    async def get_ticket_info(self, ticket_id):
        return SimpleNamespace(client_id=self._client_id)


def test_build_embedding_text_is_problem_side():
    pair = {"context": "Клиент: касса не печатает\nОператор: проверьте бумагу",
            "operator_answer": "перезагрузите"}
    text = build_embedding_text(pair)
    assert "касса не печатает" in text                 # клиентская сторона
    assert "перезагрузите" not in text                 # НЕ ответ оператора


async def test_mine_ticket_pairs_embeds_problem_side_and_counts_created():
    posts = [_post(1, "client", "вопрос один"), _post(2, "op", "ответ один")]
    saved = {}
    embedded_texts = []

    async def fake_embed(text, task_type="passage"):
        embedded_texts.append(text)
        return np.ones(4, dtype=np.float32)

    async def fake_save(**kw):
        first = kw["content_hash"] not in saved
        saved[kw["content_hash"]] = kw
        return (len(saved), first)

    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1", "type_id": "5"}, {"op"},
        _embed_fn=fake_embed, _save_fn=fake_save,
    )
    assert n == 1
    (pair,) = saved.values()
    assert pair["issue_type"] == "5" and pair["client_id"] == "client"
    assert pair["embedding_status"] == "ready"
    assert pair["embedding_model"] == "intfloat/multilingual-e5-large"
    assert "вопрос один" in embedded_texts[0]          # problem-side embedded
    assert "ответ один" not in embedded_texts[0]


async def test_mine_saves_pending_when_embed_fails():
    posts = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    saved = {}

    async def fail_embed(text, task_type="passage"):
        return None

    async def fake_save(**kw):
        first = kw["content_hash"] not in saved
        saved[kw["content_hash"]] = kw
        return (1, first)

    n = await mine_ticket_pairs(
        _FakeHDE(posts), {"id": "T1"}, {"op"},
        _embed_fn=fail_embed, _save_fn=fake_save,
    )
    assert n == 1
    (pair,) = saved.values()
    assert pair["embedding"] is None
    assert pair["embedding_status"] == "pending"       # не потеряна — повторим позже


async def test_reembed_pending_fills_missing():
    async def fake_list(limit=200):
        return [{"pair_id": 7, "context": "Клиент: q", "operator_answer": "a",
                 "embedding_text_hash": None}]

    updates = []

    async def fake_embed(text, task_type="passage"):
        return np.ones(4, dtype=np.float32)

    async def fake_set(pair_id, embedding, model, status):
        updates.append((pair_id, status))

    n = await reembed_pending(_embed_fn=fake_embed, _list_fn=fake_list, _set_fn=fake_set)
    assert n == 1
    assert updates == [(7, "ready")]


import bot.scheduler as scheduler_module


async def test_nightly_skips_when_mining_disabled(monkeypatch):
    cfg = config_module.config
    monkeypatch.setattr(cfg, "agent_dialogue_mining_enabled", False)
    called = {"mine": False}

    async def fake_mine(*a, **k):
        called["mine"] = True
        return 0

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    scheduler_module._last_dialogue_backfill_date = None
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert called["mine"] is False


async def test_nightly_independent_of_agent_enabled(monkeypatch):
    cfg = config_module.config
    # agent_enabled ON but mining flag OFF → still no mining
    monkeypatch.setattr(cfg, "agent_enabled", True)
    monkeypatch.setattr(cfg, "agent_dialogue_mining_enabled", False)
    called = {"mine": False}

    async def fake_mine(*a, **k):
        called["mine"] = True
        return 0

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    scheduler_module._last_dialogue_backfill_date = None
    await scheduler_module._maybe_backfill_dialogue_pairs(bot=None)
    assert called["mine"] is False


async def test_resolve_staff_ids_uses_api_and_cache():
    from bot.agent.dialogue_mining import resolve_staff_ids

    calls = []

    class _C:
        async def get_user_group_type(self, uid):
            calls.append(uid)
            return "staff" if uid in ("67", "98") else "client"

    cache = {}
    staff = await resolve_staff_ids(
        _C(), {"98", "67", "45425"}, base={"98"}, cache=cache
    )
    assert staff == {"98", "67"}                 # 45425 — client, не попал
    assert "98" not in calls                     # base не резолвится
    # повторный вызов — из кэша, без API
    calls.clear()
    staff2 = await resolve_staff_ids(_C(), {"67", "45425"}, base={"98"}, cache=cache)
    assert staff2 == {"98", "67"}
    assert calls == []


def test_split_records_operator_user_id():
    staff = {"op"}
    posts = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    pair = split_ticket_into_pairs("T1", posts, staff)[0]
    assert pair["operator_user_id"] == "op"


async def test_mine_passes_operator_user_id_and_resolves_staff():
    posts = [
        _post(1, "client", "вопрос"),
        _post(2, "48268", "ответ Дины"),          # staff по API, не в base
    ]
    saved = {}

    class _HDEWithUsers(_FakeHDE):
        async def get_user_group_type(self, uid):
            return "staff" if uid == "48268" else "client"

    async def fake_embed(text, task_type="passage"):
        return None

    async def fake_save(**kw):
        saved.update(kw)
        return (1, True)

    n = await mine_ticket_pairs(
        _HDEWithUsers(posts), {"id": "T1"}, {"98"},
        staff_cache={}, _embed_fn=fake_embed, _save_fn=fake_save,
    )
    assert n == 1
    assert saved["operator_user_id"] == "48268"   # автор ответа зафиксирован


# --- run_dialogue_backfill: пагинация вглубь до зоны отсечки -----------------

from datetime import datetime, timedelta, timezone


def _closed(tid, days_ago, now):
    return {"id": tid, "date_updated": (now - timedelta(days=days_ago)).isoformat()}


class _PagedClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get_closed_tickets_page(self, owner_id, page):
        self.calls.append(page)
        idx = page - 1
        if idx >= len(self.pages):
            return [], len(self.pages)
        return self.pages[idx], len(self.pages)


def _patch_backfill_db(monkeypatch, processed):
    async def hashes():
        return set()

    async def processed_ids():
        return set(processed)

    marked = []

    async def mark(tid):
        marked.append(tid)

    async def log_err(tid, msg):
        pass

    monkeypatch.setattr(scheduler_module.db, "dialogue_pair_hashes", hashes)
    monkeypatch.setattr(scheduler_module.db, "list_processed_ticket_ids", processed_ids)
    monkeypatch.setattr(scheduler_module.db, "mark_ticket_processed", mark)
    monkeypatch.setattr(scheduler_module.db, "log_ticket_error", log_err)
    return marked


async def test_backfill_walks_past_fresh_page(monkeypatch):
    """Страница 1 — свежак (<7 дней), майнимое лежит глубже. Раньше брали только
    страницу 1 и уходили с нулём."""
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    pages = [
        [_closed("F1", 1, now), _closed("F2", 2, now)],       # свежие → skip
        [_closed("N1", 8, now), _closed("N2", 9, now)],       # зона → майнить
        [_closed("O1", 20, now), _closed("O2", 21, now)],     # старые, processed
    ]
    client = _PagedClient(pages)
    marked = _patch_backfill_db(monkeypatch, processed={"O1", "O2"})

    async def fake_mine(cl, ticket, staff, **kw):
        return 2

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    stats = await scheduler_module.run_dialogue_backfill(_client=client, _now=now)
    assert marked == ["N1", "N2"]
    assert stats["new_pairs"] == 4
    assert client.calls == [1, 2, 3]              # дошёл до обработанной зоны и встал


async def test_backfill_stops_on_all_known_page(monkeypatch):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    pages = [
        [_closed("O1", 20, now), _closed("O2", 30, now)],     # всё processed
        [_closed("O3", 40, now)],
    ]
    client = _PagedClient(pages)
    _patch_backfill_db(monkeypatch, processed={"O1", "O2", "O3"})

    async def fake_mine(cl, ticket, staff, **kw):
        raise AssertionError("не должен майнить")

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    stats = await scheduler_module.run_dialogue_backfill(_client=client, _now=now)
    assert client.calls == [1]                    # история обработана → стоп сразу
    assert stats["new_pairs"] == 0


async def test_backfill_respects_max_pages(monkeypatch):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    pages = [[_closed(f"F{i}", 1, now)] for i in range(10)]   # всё свежее, стопа нет
    client = _PagedClient(pages)
    _patch_backfill_db(monkeypatch, processed=set())

    async def fake_mine(cl, ticket, staff, **kw):
        return 1

    monkeypatch.setattr("bot.agent.dialogue_mining.mine_ticket_pairs", fake_mine)
    await scheduler_module.run_dialogue_backfill(_client=client, _now=now, max_pages=2)
    assert client.calls == [1, 2]

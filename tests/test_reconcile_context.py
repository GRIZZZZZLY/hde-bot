"""Судья сверки должен видеть то, что видел оператор.

Пока промпт судьи знал только вопрос клиента, черновик и ответ оператора, любой
ответ, опирающийся на скриншот, комментарий коллеги или разговор по телефону,
размечался как ошибка бота и уезжал в очередь кандидатов в базу знаний. Владелец
(он же тот «профильный специалист», на которого бот эскалирует) получал утром
десяток решений по фактам, которые фактами не являются.
"""
import json
from types import SimpleNamespace

from bot.agent.reconcile import collect_after_anchor


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024", files=None):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text, is_comment=is_comment,
                           date_created=dc, files=files or [])


_STAFF = {"op", "colleague"}


# --- A1: сбор контекста, появившегося после якоря ---------------------------


def test_collect_after_anchor_picks_colleague_comment():
    posts = [
        _post(1, "cl", "не могу восстановить QR-код на терминале"),   # anchor
        _post(2, "colleague", "звонил в банк, QR генерирует эквайер", is_comment=True),
        _post(3, "op", "Обращайтесь в банк, Posiflora QR не генерирует"),
    ]
    got = collect_after_anchor(posts, 1, _STAFF, client_id="cl")
    assert got["colleague_comments"] == ["звонил в банк, QR генерирует эквайер"]
    assert got["client_messages"] == []


def test_collect_after_anchor_picks_late_client_message():
    posts = [
        _post(1, "cl", "касса не печатает"),                          # anchor
        _post(2, "cl", "вот фото ошибки на экране"),
        _post(3, "op", "Смените рулон, датчик бумаги залип"),
    ]
    got = collect_after_anchor(posts, 1, _STAFF, client_id="cl")
    assert got["client_messages"] == ["вот фото ошибки на экране"]


def test_collect_after_anchor_reports_attachments():
    posts = [
        _post(1, "cl", "ошибка"),                                     # anchor
        _post(2, "cl", "скриншот", files=[{"name": "error.png"}]),
        _post(3, "op", "По скриншоту это ошибка ФН, нужна замена"),
    ]
    got = collect_after_anchor(posts, 1, _STAFF, client_id="cl")
    assert got["has_attachments"] is True
    assert got["attachment_names"] == ["error.png"]


def test_collect_after_anchor_stops_at_operator_turn():
    """Появившееся ПОСЛЕ ответа оператора на его решение не влияло."""
    posts = [
        _post(1, "cl", "вопрос"),                                     # anchor
        _post(2, "colleague", "до ответа", is_comment=True),
        _post(3, "op", "Перезагрузите кассу, это залипший буфер"),
        _post(4, "colleague", "после ответа", is_comment=True),
        _post(5, "cl", "после ответа тоже"),
    ]
    got = collect_after_anchor(posts, 1, _STAFF, client_id="cl")
    assert got["colleague_comments"] == ["до ответа"]
    assert got["client_messages"] == []


def test_collect_after_anchor_ignores_everything_before_anchor():
    posts = [
        _post(1, "colleague", "старый комментарий", is_comment=True),
        _post(2, "cl", "вопрос"),                                     # anchor
        _post(3, "op", "Проверьте кабель питания принтера чеков"),
    ]
    got = collect_after_anchor(posts, 2, _STAFF, client_id="cl")
    assert got["colleague_comments"] == [] and got["client_messages"] == []


def test_collect_after_anchor_empty_without_operator_reply():
    posts = [
        _post(1, "cl", "вопрос"),                                     # anchor
        _post(2, "colleague", "комментарий", is_comment=True),
    ]
    got = collect_after_anchor(posts, 1, _STAFF, client_id="cl")
    # Оператор ещё не ответил — судить нечего, но контекст собран честно.
    assert got["colleague_comments"] == ["комментарий"]


# --- A2: промпт судьи ------------------------------------------------------


def _capture_call(verdict):
    seen = {}

    async def _call(system, user, *, model, **kwargs):
        seen["system"], seen["user"] = system, user
        return json.dumps(verdict, ensure_ascii=False)

    return _call, seen


async def test_judge_prompt_carries_colleague_comment():
    from bot.agent.reconcile import judge_divergence
    call, seen = _capture_call({"category": "context_gap", "missing": "comment",
                                "reason": "коллега звонил в банк"})
    await judge_divergence(
        "передадим специалисту", "Обращайтесь в банк",
        client_text="не восстанавливается QR",
        after_context={"colleague_comments": ["звонил в банк, QR генерирует эквайер"],
                       "client_messages": [], "attachment_names": [],
                       "has_attachments": False},
        _call_fn=call,
    )
    assert "звонил в банк" in seen["user"]
    assert "Комментарии коллег" in seen["user"]


async def test_judge_prompt_carries_photo_and_call_notes():
    from bot.agent.reconcile import judge_divergence
    call, seen = _capture_call({"category": "same_action", "reason": "ок"})
    await judge_divergence(
        "a", "b",
        photo_descriptions="на экране кассы ошибка E103",
        call_notes="в звонке выяснили: терминал от Сбера",
        _call_fn=call,
    )
    assert "E103" in seen["user"] and "от Сбера" in seen["user"]


async def test_judge_prompt_omits_empty_context_blocks():
    from bot.agent.reconcile import judge_divergence
    call, seen = _capture_call({"category": "same_action", "reason": "ок"})
    await judge_divergence("a", "b", _call_fn=call)
    for label in ("Комментарии коллег", "Вложения", "После черновика", "Из звонка"):
        assert label not in seen["user"]


async def test_judge_system_prompt_defines_context_gap_before_wrong_fact():
    """Порядок в инструкции не косметика: найдя «неверный факт», модель уже не
    переоценивает вердикт, поэтому проверка «а мог ли бот знать» должна стоять
    раньше."""
    from bot.agent.reconcile import _build_judge_prompt
    system, _ = _build_judge_prompt("a", "b", "q")
    assert "context_gap" in system
    assert system.index("context_gap") < system.index("bot_wrong_fact")


async def test_judge_accepts_context_gap_with_missing_channel():
    from bot.agent.reconcile import judge_divergence
    call, _ = _capture_call({"category": "context_gap", "missing": "screenshot",
                             "reason": "оператор смотрел скриншот"})
    verdict = await judge_divergence("a", "b", _call_fn=call)
    assert verdict == ("context_gap", "missing=screenshot оператор смотрел скриншот")


async def test_judge_context_gap_without_missing_defaults_to_other():
    from bot.agent.reconcile import judge_divergence
    call, _ = _capture_call({"category": "context_gap", "reason": "непонятно что"})
    verdict = await judge_divergence("a", "b", _call_fn=call)
    assert verdict == ("context_gap", "missing=other непонятно что")


async def test_judge_rejects_unknown_missing_channel():
    """Модель фантазирует значения перечислений — неизвестный канал сводим к
    other, а не роняем вердикт целиком."""
    from bot.agent.reconcile import judge_divergence
    call, _ = _capture_call({"category": "context_gap", "missing": "телепатия",
                             "reason": "x"})
    category, detail = await judge_divergence("a", "b", _call_fn=call)
    assert category == "context_gap" and detail.startswith("missing=other")


# --- A3: маршрутизация вердикта --------------------------------------------


async def test_context_gap_is_counted_but_not_labelled_and_never_queued():
    from bot.agent.reconcile import reconcile_recent

    async def _suggestions(hours):
        return [{"id": 7, "ticket_id": "T7", "ai_answer": "передадим специалисту",
                 "client_text": "QR не восстанавливается", "context_until_post_id": 1,
                 "title": "QR", "history": "h"}]

    async def _posts(ticket_id):
        return [
            _post(2, "colleague", "звонил в банк", is_comment=True),
            _post(3, "op", "Обращайтесь в банк, Posiflora QR не генерирует"),
        ]

    async def _judge(ai_answer, reference, **kwargs):
        return ("context_gap", "missing=comment коллега звонил в банк")

    saved, queued = [], []

    async def _set(sid, *, reference_answer, label, detail, category=None):
        saved.append((sid, label, detail, category))

    async def _candidate(**kwargs):
        queued.append(kwargs)

    async def _sleep(_):
        return None

    stats = await reconcile_recent(
        _suggestions_fn=_suggestions, _posts_fn=_posts, _set_fn=_set,
        _judge_fn=_judge, _sleep_fn=_sleep, _candidate_fn=_candidate,
        _staff={"op", "colleague"}, pause_s=0.0,
    )
    assert stats["context_gap"] == 1
    assert queued == []                       # в базу знаний такое не идёт
    assert len(saved) == 1
    sid, label, detail, category = saved[0]
    assert (sid, label, category) == (7, None, "context_gap")
    assert "missing=comment" in detail


async def test_reconcile_passes_after_anchor_context_to_judge():
    from bot.agent.reconcile import reconcile_recent
    seen = {}

    async def _suggestions(hours):
        return [{"id": 1, "ticket_id": "T1", "ai_answer": "ответ бота",
                 "client_text": "вопрос", "context_until_post_id": 1,
                 "title": "t", "history": "h"}]

    async def _posts(ticket_id):
        return [
            _post(2, "colleague", "у клиента терминал Сбера", is_comment=True),
            _post(3, "op", "Настройки терминала меняет банк, обратитесь туда"),
        ]

    async def _judge(ai_answer, reference, **kwargs):
        seen.update(kwargs)
        return ("same_action", "ок")

    async def _set(*a, **kw):
        return None

    async def _sleep(_):
        return None

    await reconcile_recent(
        _suggestions_fn=_suggestions, _posts_fn=_posts, _set_fn=_set,
        _judge_fn=_judge, _sleep_fn=_sleep, _staff={"op", "colleague"}, pause_s=0.0,
    )
    assert seen["after_context"]["colleague_comments"] == ["у клиента терминал Сбера"]


async def test_reconcile_reads_photo_and_call_notes_from_topic():
    from bot.agent.reconcile import reconcile_recent
    seen = {}

    async def _suggestions(hours):
        return [{"id": 1, "ticket_id": "T1", "ai_answer": "a",
                 "context_until_post_id": 1}]

    async def _posts(ticket_id):
        return [_post(2, "op", "По фото это ошибка ФН, нужна замена накопителя")]

    async def _topic(ticket_id):
        return SimpleNamespace(photo_descriptions="на экране ошибка ФН 235",
                               call_notes="в звонке: ФН заполнен")

    async def _judge(ai_answer, reference, **kwargs):
        seen.update(kwargs)
        return ("same_action", "ок")

    async def _set(*a, **kw):
        return None

    async def _sleep(_):
        return None

    await reconcile_recent(
        _suggestions_fn=_suggestions, _posts_fn=_posts, _set_fn=_set,
        _judge_fn=_judge, _sleep_fn=_sleep, _topic_fn=_topic,
        _staff={"op"}, pause_s=0.0,
    )
    assert seen["photo_descriptions"] == "на экране ошибка ФН 235"
    assert seen["call_notes"] == "в звонке: ФН заполнен"


async def test_topic_lookup_failure_does_not_break_reconcile():
    """Тикет без топика в базе (удалён, старый) не должен ронять сверку."""
    from bot.agent.reconcile import reconcile_recent

    async def _suggestions(hours):
        return [{"id": 1, "ticket_id": "T1", "ai_answer": "a",
                 "context_until_post_id": 1}]

    async def _posts(ticket_id):
        return [_post(2, "op", "Проверьте, открыта ли смена на кассе")]

    async def _topic(ticket_id):
        raise RuntimeError("нет такого топика")

    async def _judge(ai_answer, reference, **kwargs):
        return ("same_action", "ок")

    async def _set(*a, **kw):
        return None

    async def _sleep(_):
        return None

    stats = await reconcile_recent(
        _suggestions_fn=_suggestions, _posts_fn=_posts, _set_fn=_set,
        _judge_fn=_judge, _sleep_fn=_sleep, _topic_fn=_topic,
        _staff={"op"}, pause_s=0.0,
    )
    assert stats == {"same_action": 1, "bot_escalated": 0, "bot_wrong_fact": 0,
                     "context_gap": 0, "not_comparable": 0, "skipped": 0, "errors": 0}


# --- A3-контракт: метка не ставится, датасет оценки не засоряется ----------


async def test_set_judge_result_with_none_label_keeps_category_only():
    """context_gap обязан попасть в сводку (категория) и НЕ попасть в датасет
    офлайн-оценки (метка): эталон есть, но черновик по нему оценивать нельзя."""
    import bot.db as db_module
    from bot.db.suggestion_store import (
        get_evaluation_samples, get_suggestion, record_suggestion, set_judge_result,
    )
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="GAP1", topic_id=1, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
        history="история", ai_answer="черновик",
    )
    await set_judge_result(
        sid, reference_answer="Обращайтесь в банк", label=None,
        detail="judge:context_gap missing=comment коллега звонил", category="context_gap",
    )
    row = await get_suggestion(sid)
    assert row["judge_category"] == "context_gap"
    assert row["judge_label"] is None
    assert row["effective_label"] is None
    assert row["judged_at"] is not None            # второй раз судить не нужно
    assert row["judge_reference_answer"] == "Обращайтесь в банк"
    assert all(s["ticket_id"] != "GAP1" for s in await get_evaluation_samples())


# --- A4: сводка ------------------------------------------------------------


async def test_digest_exposes_context_gap_rows():
    import bot.db as db_module
    from bot.db.suggestion_store import (
        get_reconciliation_digest, record_suggestion, set_judge_result,
    )
    await db_module.init_db()
    sid = await record_suggestion(
        ticket_id="GAP2", topic_id=1, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
        ai_answer="передадим специалисту",
    )
    await set_judge_result(
        sid, reference_answer="Обращайтесь в банк", label=None,
        detail="judge:context_gap missing=screenshot оператор смотрел скриншот",
        category="context_gap",
    )
    d = await get_reconciliation_digest(hours=24)
    assert d["counts"].get("context_gap") == 1
    assert [r["ticket_id"] for r in d["context_gap"]] == ["GAP2"]
    # judged считает вердикты, а не метки: иначе «бот не мог знать» пропадает
    # из счётчика и сводка выглядит недоработавшей.
    assert d["judged"] == 1


def test_format_digest_shows_context_gap_breakdown():
    from bot.formatter import format_reconciliation_digest
    txt = format_reconciliation_digest({
        "judged": 6,
        "counts": {"same_action": 3, "bot_wrong_fact": 1, "context_gap": 2},
        "escalated": [],
        "wrong_fact": [{"ticket_id": "1", "ai_answer": "a",
                        "judge_reference_answer": "b",
                        "judge_detail": "judge:bot_wrong_fact неверно"}],
        "context_gap": [
            {"ticket_id": "200100", "ai_answer": "передадим специалисту",
             "judge_reference_answer": "Обращайтесь в банк",
             "judge_detail": "judge:context_gap missing=comment коллега звонил"},
            {"ticket_id": "200101", "ai_answer": "уточните модель",
             "judge_reference_answer": "По фото это ФН",
             "judge_detail": "judge:context_gap missing=screenshot смотрел фото"},
        ],
        "trend": [],
    })
    assert "не мог знать" in txt
    assert "200100" in txt and "200101" in txt
    # разбивка по каналам: понятно, какого именно контекста не хватает
    assert "коммент" in txt.lower() and "скрин" in txt.lower()


def test_format_digest_context_gap_only_still_reports():
    """Все вердикты — context_gap: расхождений «по вине бота» нет, но молчать
    нельзя, иначе не видно, что канал контекста дырявый."""
    from bot.formatter import format_reconciliation_digest
    txt = format_reconciliation_digest({
        "judged": 3, "counts": {"context_gap": 3},
        "escalated": [], "wrong_fact": [],
        "context_gap": [{"ticket_id": "1", "ai_answer": "a",
                         "judge_reference_answer": "b",
                         "judge_detail": "judge:context_gap missing=call звонок"}],
        "trend": [],
    })
    assert txt is not None and "не мог знать" in txt

import json
from types import SimpleNamespace

from bot.agent.reconcile import (
    find_operator_reply_after,
    reconcile_one,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


_STAFF = {"op"}


def _ref(operator_text, anchor=1):
    """Эталон из одного ответа оператора после якоря."""
    posts = [_post(anchor, "cl", "вопрос клиента"), _post(anchor + 1, "op", operator_text)]
    return find_operator_reply_after(posts, anchor, _STAFF)


def test_finds_first_operator_turn_after_anchor():
    posts = [
        _post(1, "cl", "касса не печатает чек"),
        _post(2, "cl", "что делать?"),                       # anchor
        _post(3, "op", "Перезагрузите кассу и попробуйте снова"),
        _post(4, "cl", "спасибо"),
    ]
    ref = find_operator_reply_after(posts, 2, _STAFF)
    assert ref == "Перезагрузите кассу и попробуйте снова"


def test_boilerplate_before_real_answer_is_stripped():
    posts = [
        _post(1, "cl", "терминал не проводит оплату"),       # anchor
        _post(2, "op", "Благодарим за информацию, передано профильному специалисту"),
        _post(3, "op", "Перезагрузите роутер и терминал, повторите оплату"),
    ]
    ref = find_operator_reply_after(posts, 1, _STAFF)
    assert ref == "Перезагрузите роутер и терминал, повторите оплату"


def test_none_when_no_operator_reply_yet():
    posts = [
        _post(1, "cl", "касса не работает"),                 # anchor
        _post(2, "cl", "ещё сообщение клиента"),
    ]
    assert find_operator_reply_after(posts, 1, _STAFF) is None


def test_none_when_only_boilerplate():
    posts = [
        _post(1, "cl", "вопрос"),                            # anchor
        _post(2, "op", "Обращение принято в работу, специалист свяжется с вами"),
    ]
    assert find_operator_reply_after(posts, 1, _STAFF) is None


def test_none_when_only_closer():
    posts = [
        _post(1, "cl", "вопрос"),                            # anchor
        _post(2, "op", "Всегда рады помочь! Будут ещё вопросы — обращайтесь."),
    ]
    assert find_operator_reply_after(posts, 1, _STAFF) is None


def test_closer_stripped_from_real_answer():
    posts = [
        _post(1, "cl", "подключите кассу"),                  # anchor
        _post(2, "op", "Готово, касса подключена. Всегда рады помочь! "
                       "Будут ещё вопросы — обращайтесь."),
    ]
    assert find_operator_reply_after(posts, 1, _STAFF) == "Готово, касса подключена."


def test_closer_question_variant_stripped():
    posts = [
        _post(1, "cl", "терминал не работает"),              # anchor
        _post(2, "op", "Сбились настройки терминала, обратитесь в банк. "
                       "Подскажите, могу вам ещё чем-то помочь?"),
    ]
    ref = find_operator_reply_after(posts, 1, _STAFF)
    assert ref == "Сбились настройки терминала, обратитесь в банк."


# --- Отсев реплик, которые не являются ответом на вопрос клиента ------------
# Все строки ниже — дословные эталоны из прода (ai_suggestions за 30 дней).


def test_remote_session_macro_only_is_not_an_answer():
    assert _ref("Примите запрос на компьютере") is None
    assert _ref("Примите запрос на компьютере, я к вам подключаюсь") is None
    assert _ref("Примите запрос ещё раз") is None


def test_remote_session_macro_stripped_but_content_kept():
    assert _ref("Примите запрос на компьютере Обновил вам драйвер. "
                "Попробуйте сейчас напечатать чек") == (
        "Обновил вам драйвер. Попробуйте сейчас напечатать чек"
    )


def test_remote_access_instruction_is_a_real_answer():
    """Просьба поставить AnyDesk — ответ клиенту, а не обслуживание сеанса.

    Если бот вместо этого отправил ждать специалиста, это осмысленное
    расхождение (bot_escalated), и судья должен его увидеть. LLM-гейт пар
    независимо согласен: 22 таких ответа в прод-выборке помечены auto_accepted.
    """
    text = (
        "Необходимо удаленно подключиться к вашему компьютеру. Скачайте программу "
        "для удаленного доступа AnyDesk на ваш компьютер по ссылке: "
        "https://anydesk.com/ru Запустите ее и пришлите номер рабочего места."
    )
    assert _ref(text) == text


def test_promise_stripped_but_remote_access_instruction_kept():
    assert _ref("Через 15-20 минут, свяжусь с вами Скачайте программу для "
                "удаленного доступа AnyDesk и пришлите номер рабочего места.") == (
        "Скачайте программу для удаленного доступа AnyDesk и пришлите "
        "номер рабочего места."
    )


def test_connection_failure_macro_is_not_an_answer():
    assert _ref("Не могу к вам подключиться, интернет есть на компьютере?") is None
    assert _ref("Не смог вам дозвониться, есть другой номер для связи?") is None
    assert _ref("По какому номеру могу с вами связаться?") is None


def test_connection_failure_macro_survives_typo_and_phone():
    assert _ref("Не могу к вам подключиться, интерент есть на компьютере? "
                "По этому номеру не могу вам дозвониться 89600518688 "
                "Есть другой номер для связи?") is None


def test_callback_arrangement_is_not_an_answer():
    assert _ref("Не смог вам дозвониться, напишите когда могу перезвонить "
                "или другой номер для связи") is None
    assert _ref("Связался с инженером, напишите, когда нужно будет перезвонить") is None


def test_punctuation_left_by_filters_is_not_kept():
    assert _ref("Подскажите, кассу Эвотор и Wifi роутер перезагрузили? "
                "Не получил ответ на последнее сообщение. Если возникнут вопросы, "
                "напишите мне снова, я буду здесь.") == (
        "Подскажите, кассу Эвотор и Wifi роутер перезагрузили?"
    )


def test_no_reply_chase_is_not_an_answer():
    assert _ref("Не получил ответ на последнее сообщение. Если возникнут вопросы, "
                "напишите мне снова, я буду здесь.") is None
    assert _ref("Подскажите, вопрос актуален?") is None
    assert _ref("Скажите, пожалуйста, вопрос еще актуален? Ждем ответа, "
                "чтобы помочь вам!") is None


def test_callback_promise_is_not_an_answer():
    assert _ref("Через 15-20 минут, подключусь к вам") is None
    assert _ref("Перезвоню вам через 20 минут") is None
    assert _ref("Связался") is None
    assert _ref("Подключился к вам") is None


def test_ack_only_is_not_an_answer():
    assert _ref("Хорошо") is None
    assert _ref("Хорошо, спасибо") is None
    assert _ref("Отлично.") is None


def test_emoji_only_is_not_an_answer():
    assert _ref("🤝") is None
    assert _ref("✍") is None


def test_marketing_broadcast_is_not_an_answer():
    assert _ref(
        "Витрина стала современнее: новый заказ на одной странице, четкий логотип, "
        "баннер для акций, простые настройки дизайна и скрытие букетов без фото."
    ) is None


def test_short_real_instruction_survives_the_filter():
    assert _ref("Пробуйте печатать чек") == "Пробуйте печатать чек"
    assert _ref("Откройте сейчас смену, пожалуйста") == "Откройте сейчас смену, пожалуйста"


# --- LLM-судья --------------------------------------------------------------


def _call_returning(obj):
    async def _call(system, user, *, model, **kwargs):
        return json.dumps(obj, ensure_ascii=False)
    return _call


async def test_judge_returns_category_and_reason():
    from bot.agent.reconcile import judge_divergence
    verdict = await judge_divergence(
        "Перезагрузите кассу", "Выполните перезагрузку кассы",
        _call_fn=_call_returning({"category": "same_action", "reason": "то же действие"}),
    )
    assert verdict == ("same_action", "то же действие")


async def test_judge_none_on_unknown_category():
    from bot.agent.reconcile import judge_divergence
    assert await judge_divergence(
        "a", "b", _call_fn=_call_returning({"category": "whatever", "reason": ""}),
    ) is None


async def test_judge_none_on_broken_json():
    from bot.agent.reconcile import judge_divergence

    async def _call(system, user, *, model, **kwargs):
        return "не json"

    assert await judge_divergence("a", "b", _call_fn=_call) is None


def _judge_returning(*verdicts):
    """Судья, отдающий вердикты по очереди (последний повторяется)."""
    calls = []

    async def _judge(ai_answer, reference, **kwargs):
        calls.append((ai_answer, reference))
        return verdicts[min(len(calls) - 1, len(verdicts) - 1)]

    _judge.calls = calls
    return _judge


async def test_reconcile_one_returns_category_and_reason():
    posts = [
        _post(1, "cl", "касса зависла"),                     # anchor
        _post(2, "op", "Перезагрузите кассу и попробуйте снова"),
    ]
    res = await reconcile_one(
        ai_answer="Выполните перезагрузку кассы",
        posts=posts, anchor_post_id=1, staff=_STAFF,
        _judge_fn=_judge_returning(("same_action", "то же действие")),
    )
    assert res == {
        "reference_answer": "Перезагрузите кассу и попробуйте снова",
        "category": "same_action",
        "reason": "то же действие",
    }


async def test_reconcile_one_none_when_reference_is_a_macro():
    posts = [_post(1, "cl", "вопрос"), _post(2, "op", "Примите запрос на компьютере")]
    judge = _judge_returning(("same_action", ""))
    assert await reconcile_one(
        ai_answer="любой", posts=posts, anchor_post_id=1, staff=_STAFF, _judge_fn=judge,
    ) is None
    assert judge.calls == []  # судью не зовём, сравнивать нечего


async def test_reconcile_one_none_without_reply():
    posts = [_post(1, "cl", "вопрос")]
    assert await reconcile_one(
        ai_answer="любой", posts=posts, anchor_post_id=1, staff=_STAFF,
        _judge_fn=_judge_returning(("same_action", "")),
    ) is None


async def test_reconcile_recent_maps_categories_to_labels():
    suggestions = [
        {"id": 1, "ticket_id": "T1", "ai_answer": "Выполните перезагрузку кассы",
         "context_until_post_id": "1"},
        {"id": 2, "ticket_id": "T2", "ai_answer": "Специалист свяжется с вами",
         "context_until_post_id": "1"},
        {"id": 3, "ticket_id": "T3", "ai_answer": "Настройку делает только банк",
         "context_until_post_id": "1"},
        {"id": 4, "ticket_id": "T4", "ai_answer": "Пришлите фото чека",
         "context_until_post_id": "1"},
        {"id": 5, "ticket_id": "T5", "ai_answer": "любой", "context_until_post_id": "1"},
        {"id": 6, "ticket_id": "T6", "ai_answer": "любой", "context_until_post_id": "1"},
    ]
    posts_by = {
        "T1": [_post(1, "cl", "q"), _post(2, "op", "Перезагрузите кассу")],
        "T2": [_post(1, "cl", "q"), _post(2, "op", "Обновил драйвер, пробуйте печатать чек")],
        "T3": [_post(1, "cl", "q"), _post(2, "op", "Подключился и настроил терминал сам")],
        "T4": [_post(1, "cl", "q"), _post(2, "op", "Какой IP-адрес прописан в настройках?")],
        "T5": [_post(1, "cl", "q")],                                   # оператор молчит
        "T6": [_post(1, "cl", "q"), _post(2, "op", "Примите запрос на компьютере")],
    }
    verdicts = {
        "T1": ("same_action", "то же"),
        "T2": ("bot_escalated", "оператор решил сам"),
        "T3": ("bot_wrong_fact", "банк тут не нужен"),
        "T4": ("not_comparable", "про разное"),
    }

    async def sug_fn(hours):
        return suggestions

    async def posts_fn(tid):
        return posts_by[tid]

    async def judge_fn(ai_answer, reference, **kwargs):
        tid = next(t for t, p in posts_by.items() if len(p) > 1 and reference in p[1].text)
        return verdicts[tid]

    saved = []

    async def set_fn(sid, *, reference_answer, label, detail, category=None):
        saved.append((sid, label, detail, category))

    async def sleep_fn(_seconds):
        return None

    from bot.agent.reconcile import reconcile_recent
    stats = await reconcile_recent(
        hours=24, _suggestions_fn=sug_fn, _posts_fn=posts_fn, _set_fn=set_fn,
        _staff={"op"}, _judge_fn=judge_fn, _sleep_fn=sleep_fn,
    )
    assert stats == {
        "same_action": 1, "bot_escalated": 1, "bot_wrong_fact": 1,
        "context_gap": 0, "not_comparable": 1, "skipped": 2, "errors": 0,
    }
    assert [(sid, label) for sid, label, _, _ in saved] == [
        (1, "accepted"), (2, "corrected"), (3, "corrected"),
    ]
    assert saved[1][2].startswith("judge:bot_escalated")
    # Категория — отдельным полем: по ней строится утренняя сводка (п.3).
    assert [cat for _, _, _, cat in saved] == [
        "same_action", "bot_escalated", "bot_wrong_fact",
    ]


async def test_reconcile_recent_retries_judge_once_then_skips():
    suggestions = [{"id": 1, "ticket_id": "T1", "ai_answer": "текст",
                    "context_until_post_id": "1"}]
    posts = [_post(1, "cl", "q"), _post(2, "op", "Обновил драйвер, пробуйте печатать чек")]

    async def sug_fn(hours):
        return suggestions

    async def posts_fn(tid):
        return posts

    calls = []

    async def judge_fn(ai_answer, reference, **kwargs):
        calls.append(reference)
        return None

    saved = []

    async def set_fn(sid, **kwargs):
        saved.append(sid)

    async def sleep_fn(_seconds):
        return None

    from bot.agent.reconcile import reconcile_recent
    stats = await reconcile_recent(
        hours=24, _suggestions_fn=sug_fn, _posts_fn=posts_fn, _set_fn=set_fn,
        _staff={"op"}, _judge_fn=judge_fn, _sleep_fn=sleep_fn,
    )
    assert len(calls) == 2 and saved == []
    assert stats["skipped"] == 1


async def test_reconciliation_digest_groups_by_category():
    import bot.db as db_module
    from bot.db.suggestion_store import (
        get_reconciliation_digest, record_suggestion, set_judge_result,
    )
    await db_module.init_db()

    async def _sug(ticket):
        return await record_suggestion(
            ticket_id=ticket, topic_id=1, trigger_source="first",
            context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
            ai_answer=f"черновик для {ticket}",
        )

    ok, esc, wrong = await _sug("A"), await _sug("B"), await _sug("C")
    await set_judge_result(ok, reference_answer="то же самое", label="accepted",
                           detail="judge:same_action ок", category="same_action")
    await set_judge_result(esc, reference_answer="Обновил драйвер, пробуйте",
                           label="corrected", detail="judge:bot_escalated решил сам",
                           category="bot_escalated")
    await set_judge_result(wrong, reference_answer="Настроили без банка",
                           label="corrected", detail="judge:bot_wrong_fact банк не нужен",
                           category="bot_wrong_fact")

    d = await get_reconciliation_digest(hours=24)
    assert d["judged"] == 3
    assert d["counts"] == {"same_action": 1, "bot_escalated": 1, "bot_wrong_fact": 1}
    assert [r["ticket_id"] for r in d["escalated"]] == ["B"]
    assert [r["ticket_id"] for r in d["wrong_fact"]] == ["C"]
    assert d["wrong_fact"][0]["judge_reference_answer"] == "Настроили без банка"
    assert d["wrong_fact"][0]["ai_answer"] == "черновик для C"


async def test_reconciliation_digest_trend_is_seven_days_oldest_first():
    import bot.db as db_module
    from bot.db.core import connect
    from bot.db.suggestion_store import (
        get_reconciliation_digest, record_suggestion, set_judge_result,
    )
    await db_module.init_db()
    for ticket, category, label, days_ago in [
        ("D1", "same_action", "accepted", 0),
        ("D2", "bot_escalated", "corrected", 0),
        ("D3", "bot_wrong_fact", "corrected", 3),
        ("D4", "same_action", "accepted", 30),      # вне окна тренда
    ]:
        sid = await record_suggestion(
            ticket_id=ticket, topic_id=1, trigger_source="first",
            context_until_post_id="1", pipeline_version="v0", prompt_version="legacy",
            ai_answer="черновик",
        )
        await set_judge_result(sid, reference_answer="эталон", label=label,
                               detail=f"judge:{category}", category=category)
        if days_ago:
            async with connect() as db:
                await db.execute(
                    "UPDATE ai_suggestions SET judged_at=datetime('now', ?) WHERE id=?",
                    (f"-{days_ago} days", sid),
                )
                await db.commit()

    d = await get_reconciliation_digest(hours=24)
    trend = d["trend"]
    assert [t["diverged"] for t in trend][-1] == 1        # сегодня: одно расхождение
    assert sum(t["judged"] for t in trend) == 3           # 30-дневный вердикт не в окне
    assert [t["day"] for t in trend] == sorted(t["day"] for t in trend)  # старый→новый


def test_format_reconciliation_digest_silent_without_verdicts():
    from bot.formatter import format_reconciliation_digest
    assert format_reconciliation_digest(
        {"judged": 0, "counts": {}, "escalated": [], "wrong_fact": [], "trend": []}
    ) is None


def test_format_reconciliation_digest_keeps_heartbeat_without_divergences():
    """Расхождений нет — сводка не молчит, а отчитывается одной строкой.

    Полная тишина неотличима от «ночная задача не запустилась», а это уже
    случалось (отпускной отчёт 17.08).
    """
    from bot.formatter import format_reconciliation_digest
    txt = format_reconciliation_digest({
        "judged": 4, "counts": {"same_action": 4},
        "escalated": [], "wrong_fact": [], "trend": [],
    })
    assert txt is not None
    assert "4" in txt and "расхождений нет" in txt.lower()


def test_format_reconciliation_digest_lists_divergences_with_links(monkeypatch):
    import bot.config as config_module
    from bot.formatter import format_reconciliation_digest
    monkeypatch.setattr(
        config_module.config, "hde_api_base_url",
        "https://posiflora.helpdeskeddy.com/api/v2", raising=False,
    )
    txt = format_reconciliation_digest({
        "judged": 5,
        "counts": {"same_action": 3, "bot_escalated": 1, "bot_wrong_fact": 1},
        "escalated": [{"ticket_id": "190098", "ai_answer": "передадим специалисту",
                       "judge_reference_answer": "Обновил драйвер, пробуйте",
                       "judge_detail": "judge:bot_escalated решил сам"}],
        "wrong_fact": [{"ticket_id": "188203", "ai_answer": "настройку делает только банк",
                        "judge_reference_answer": "Подключился и настроил сам",
                        "judge_detail": "judge:bot_wrong_fact банк не нужен"}],
        "trend": [{"day": "2026-08-17", "judged": 5, "diverged": 2},
                  {"day": "2026-08-18", "judged": 5, "diverged": 2}],
    })
    assert "posiflora.helpdeskeddy.com/ru/ticket/list/filter/id/1/ticket/190098" in txt
    assert "188203" in txt
    assert "решил сам" in txt and "банк не нужен" in txt


def test_hde_ticket_url_without_configured_base(monkeypatch):
    import bot.config as config_module
    from bot.formatter import hde_ticket_url
    monkeypatch.setattr(config_module.config, "hde_api_base_url", "", raising=False)
    assert hde_ticket_url("123") == ""


async def test_judge_prompt_carries_client_question():
    """Без вопроса клиента судья сравнивал два ответа в вакууме и не мог
    отличить «бот ответил не на то» от «оператор ушёл в свою ветку»."""
    from bot.agent.reconcile import judge_divergence
    seen = {}

    async def _call(system, user, *, model, **kwargs):
        seen["user"] = user
        seen["system"] = system
        return json.dumps({"category": "same_action", "reason": "ок"})

    await judge_divergence(
        "Перезагрузите кассу", "Выполните перезагрузку",
        client_text="Касса не печатает чеки после обновления", _call_fn=_call,
    )
    assert "Касса не печатает чеки после обновления" in seen["user"]
    assert "Вопрос клиента" in seen["user"]


async def test_judge_prompt_omits_empty_question_block():
    from bot.agent.reconcile import judge_divergence
    seen = {}

    async def _call(system, user, *, model, **kwargs):
        seen["user"] = user
        return json.dumps({"category": "same_action", "reason": "ок"})

    await judge_divergence("a", "b", _call_fn=_call)
    assert "Вопрос клиента" not in seen["user"]


async def test_judge_prompt_narrows_wrong_fact_to_real_facts():
    """Сводка 2026-09-03 звала bot_wrong_fact на лишний шаг («печать
    X-отчёта») — разница в объёме, а не ошибка факта. Такие пары уезжали в
    очередь кандидатов в базу знаний, где им не место."""
    from bot.agent.reconcile import _build_judge_prompt
    system, _ = _build_judge_prompt("a", "b", "q")
    assert "НЕ bot_wrong_fact" in system
    assert "same_action" in system
    assert "not_comparable" in system


async def test_reconcile_recent_passes_client_text_to_judge():
    from bot.agent.reconcile import reconcile_recent
    seen = {}

    async def _suggestions(hours):
        return [{"id": 1, "ticket_id": "T1", "ai_answer": "ответ бота",
                 "client_text": "терминал не отвечает", "context_until_post_id": 1,
                 "title": "t", "history": "h"}]

    async def _posts(ticket_id):
        return [_post(2, "op", "Пропишите порт 8888 в настройках терминала")]

    async def _judge(ai_answer, reference, **kwargs):
        seen["client_text"] = kwargs.get("client_text")
        return ("same_action", "ок")

    async def _set(*a, **kw):
        return None

    async def _sleep(_):
        return None

    stats = await reconcile_recent(
        _suggestions_fn=_suggestions, _posts_fn=_posts, _set_fn=_set,
        _judge_fn=_judge, _sleep_fn=_sleep, _staff={"op"}, pause_s=0.0,
    )
    assert seen["client_text"] == "терминал не отвечает"
    assert stats["same_action"] == 1

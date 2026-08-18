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


def test_remote_access_instruction_macro_is_not_an_answer():
    assert _ref(
        "Необходимо удаленно подключиться к вашему компьютеру. Скачайте программу "
        "для удаленного доступа AnyDesk на ваш компьютер по ссылке: "
        "https://anydesk.com/ru Запустите ее и пришлите номер рабочего места."
    ) is None


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

    async def set_fn(sid, *, reference_answer, label, detail):
        saved.append((sid, label, detail))

    async def sleep_fn(_seconds):
        return None

    from bot.agent.reconcile import reconcile_recent
    stats = await reconcile_recent(
        hours=24, _suggestions_fn=sug_fn, _posts_fn=posts_fn, _set_fn=set_fn,
        _staff={"op"}, _judge_fn=judge_fn, _sleep_fn=sleep_fn,
    )
    assert stats == {
        "same_action": 1, "bot_escalated": 1, "bot_wrong_fact": 1,
        "not_comparable": 1, "skipped": 2, "errors": 0,
    }
    assert [(sid, label) for sid, label, _ in saved] == [
        (1, "accepted"), (2, "corrected"), (3, "corrected"),
    ]
    assert saved[1][2].startswith("judge:bot_escalated")


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


async def test_reconciliation_digest_counts_and_top():
    import bot.db as db_module
    from bot.db.suggestion_store import (
        get_reconciliation_digest, record_suggestion, set_judge_result,
    )
    await db_module.init_db()
    s1 = await record_suggestion(ticket_id="A", topic_id=1, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy")
    s2 = await record_suggestion(ticket_id="B", topic_id=2, trigger_source="first",
        context_until_post_id="1", pipeline_version="v0", prompt_version="legacy")
    await set_judge_result(s1, reference_answer="норм", label="accepted", detail="x")
    await set_judge_result(s2, reference_answer="Позвоните в банк 900", label="corrected", detail="x")
    d = await get_reconciliation_digest(hours=24)
    assert d["matched"] == 1 and d["diverged"] == 1
    assert len(d["top_diverged"]) == 1
    assert d["top_diverged"][0]["ticket_id"] == "B"
    assert d["top_diverged"][0]["judge_reference_answer"] == "Позвоните в банк 900"


def test_format_reconciliation_digest():
    from bot.formatter import format_reconciliation_digest
    assert format_reconciliation_digest({"matched": 0, "diverged": 0, "top_diverged": []}) is None
    txt = format_reconciliation_digest({
        "matched": 3, "diverged": 2,
        "top_diverged": [{"ticket_id": "T9", "ai_answer": "бот текст",
                          "judge_reference_answer": "опер текст"}],
    })
    assert "Совпало с оператором" in txt and "3" in txt
    assert "T9" in txt and "опер текст" in txt

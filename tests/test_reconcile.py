from types import SimpleNamespace

from bot.agent.reconcile import (
    compare,
    find_operator_reply_after,
    reconcile_one,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


_STAFF = {"op"}


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


def test_compare_matched_and_diverged():
    label, score = compare("Перезагрузите кассу", "Перезагрузите кассу")
    assert label == "matched" and score == 1.0
    label, score = compare("Перезагрузите кассу", "Позвоните в банк по номеру 900")
    assert label == "diverged" and score < 0.6


def test_reconcile_one_matched():
    posts = [
        _post(1, "cl", "касса зависла"),                     # anchor
        _post(2, "op", "Перезагрузите кассу и попробуйте снова"),
    ]
    res = reconcile_one(
        ai_answer="Перезагрузите кассу и попробуйте снова",
        posts=posts, anchor_post_id=1, staff=_STAFF,
    )
    assert res["label"] == "matched"
    assert res["reference_answer"] == "Перезагрузите кассу и попробуйте снова"
    assert res["score"] == 1.0


def test_reconcile_one_none_without_reply():
    posts = [_post(1, "cl", "вопрос")]
    assert reconcile_one(
        ai_answer="любой", posts=posts, anchor_post_id=1, staff=_STAFF
    ) is None


async def test_reconcile_recent_labels_counts_and_mapping():
    suggestions = [
        {"id": 1, "ticket_id": "T1", "ai_answer": "Перезагрузите кассу и попробуйте снова",
         "context_until_post_id": "1"},
        {"id": 2, "ticket_id": "T2", "ai_answer": "Проверьте бумагу",
         "context_until_post_id": "1"},
        {"id": 3, "ticket_id": "T3", "ai_answer": "любой", "context_until_post_id": "1"},
    ]
    posts_by = {
        "T1": [_post(1, "cl", "q"), _post(2, "op", "Перезагрузите кассу и попробуйте снова")],
        "T2": [_post(1, "cl", "q"), _post(2, "op", "Позвоните в банк 900 и уточните лимит по карте")],
        "T3": [_post(1, "cl", "q")],  # оператор ещё не ответил → skip
    }

    async def sug_fn(hours):
        return suggestions

    async def posts_fn(tid):
        return posts_by[tid]

    saved = []

    async def set_fn(sid, *, reference_answer, label, detail):
        saved.append((sid, label))

    from bot.agent.reconcile import reconcile_recent
    stats = await reconcile_recent(
        hours=24, _suggestions_fn=sug_fn, _posts_fn=posts_fn,
        _set_fn=set_fn, _staff={"op"},
    )
    assert stats["matched"] == 1 and stats["diverged"] == 1 and stats["skipped"] == 1
    assert dict(saved) == {1: "accepted", 2: "corrected"}  # matched→accepted, diverged→corrected; T3 не сохранён


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

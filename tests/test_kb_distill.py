"""Дистиллятор правил: конец полуручного утра.

До этой правки кандидат в базу знаний был сырым диалогом — тема, история и
дословный ответ оператора, — и по каждому владелец жал «в базу» или «мимо».
Очередь 19 pending при 2 решённых, и большинство строк правилом не является:
«Да, забыть устройство», «Нет, обновляю драйвер». Решать такое руками — работа,
которую невозможно доделать.

Теперь по каждому вердикту работает выжимка: обобщаемое правило или ничего.
Человеку остаётся один класс — противоречие с тем, что уже лежит в базе.
"""
import json

from bot.agent.kb_distill import (
    build_rule_content,
    classify_overlap,
    distill_rule,
    rule_content_hash,
)


def _call(obj):
    seen = {}

    async def _fn(system, user, *, model, **kwargs):
        seen["system"], seen["user"] = system, user
        return json.dumps(obj, ensure_ascii=False)

    return _fn, seen


# --- B1: выжимка ------------------------------------------------------------


async def test_distill_returns_rule():
    call, seen = _call({
        "generalizable": True,
        "symptom": "клиент просит восстановить QR-код на терминале",
        "rule": "QR-код на платёжном терминале генерирует банк-эквайер",
        "action": "направить в банк, выпустивший терминал",
    })
    rule = await distill_rule(
        client_text="не могу восстановить QR", ai_answer="передадим специалисту",
        reference="Обращайтесь в банк, Posiflora QR не генерирует",
        title="QR", history="h", _call_fn=call,
    )
    assert rule["symptom"].startswith("клиент просит")
    assert "банк-эквайер" in rule["rule"]
    assert "Обращайтесь в банк" in seen["user"]


async def test_distill_returns_none_for_non_generalizable():
    """«Да, забыть устройство» правилом не является — и человеку не показывается."""
    call, _ = _call({"generalizable": False})
    assert await distill_rule(
        client_text="?", ai_answer="a", reference="Да, забыть устройство",
        _call_fn=call,
    ) is None


async def test_distill_returns_none_without_rule_text():
    """generalizable=true с пустым правилом — брак модели, а не правило."""
    call, _ = _call({"generalizable": True, "symptom": "s", "rule": "", "action": "a"})
    assert await distill_rule(client_text="q", ai_answer="a", reference="r",
                              _call_fn=call) is None


async def test_distill_returns_none_on_broken_json():
    async def _fn(system, user, *, model, **kwargs):
        return "не json"

    assert await distill_rule(client_text="q", ai_answer="a", reference="r",
                              _call_fn=_fn) is None


async def test_rule_content_is_short_and_has_no_dialogue():
    """Правило в базе — три строки, а не пересказ тикета: диалог в контенте
    ломает и эмбеддинг, и чтение человеком."""
    content = build_rule_content(
        {"symptom": "QR на терминале", "rule": "генерирует банк",
         "action": "направить в банк"},
        ticket_id="200100",
    )
    assert "QR на терминале" in content and "200100" in content
    assert len(content) < 400
    assert "Клиент:" not in content


def test_rule_hash_is_stable_across_wording_noise():
    a = rule_content_hash({"rule": "QR-код генерирует БАНК-эквайер"})
    b = rule_content_hash({"rule": "  qr-код   генерирует банк-эквайер  "})
    assert a == b
    assert a != rule_content_hash({"rule": "QR-код генерирует ОФД"})


# --- B2: дедуп и конфликт ---------------------------------------------------


async def test_overlap_duplicate_detected():
    call, seen = _call({"relation": "duplicate", "reason": "то же самое"})
    assert await classify_overlap("новое", "старое", _call_fn=call) == (
        "duplicate", "то же самое",
    )
    assert "новое" in seen["user"] and "старое" in seen["user"]


async def test_overlap_unknown_relation_falls_back_to_conflict():
    """Неизвестное отношение трактуем как противоречие: показать человеку
    лишнее правило дешевле, чем автоматически записать неверное."""
    call, _ = _call({"relation": "телепатия", "reason": "x"})
    relation, _ = await classify_overlap("a", "b", _call_fn=call)
    assert relation == "contradicts"


async def test_overlap_llm_failure_falls_back_to_conflict():
    async def _fn(system, user, *, model, **kwargs):
        return None

    relation, _ = await classify_overlap("a", "b", _call_fn=_fn)
    assert relation == "contradicts"


# --- B3: проведение решения -------------------------------------------------


def _candidate(cid=1, ticket="T1"):
    return {"id": cid, "ticket_id": ticket, "title": "Тема",
            "history": "Клиент: ...\nОператор: ...",
            "ai_answer": "передадим специалисту",
            "reference_answer": "Обращайтесь в банк, Posiflora QR не генерирует",
            "reason": "не тот адресат", "status": "pending"}


async def test_new_rule_is_indexed_automatically():
    from bot.agent.kb_distill import process_candidate

    indexed, statuses = [], []

    async def _distill(**kwargs):
        return {"symptom": "QR на терминале", "rule": "генерирует банк",
                "action": "в банк"}

    async def _find_similar_rule(rule_text):
        return None                      # похожего правила в базе нет

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 77

    async def _set_status(cid, status, **kwargs):
        statuses.append((cid, status))
        return {"id": cid}

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _similar_fn=_find_similar_rule,
        _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome == "auto_added"
    assert statuses == [(1, "auto_added")]
    assert indexed[0]["source"] == "auto_rule"
    assert indexed[0]["quality"] == "auto_rule"
    assert "генерирует банк" in indexed[0]["content"]


async def test_non_generalizable_candidate_is_closed_without_human():
    from bot.agent.kb_distill import process_candidate

    indexed, statuses = [], []

    async def _distill(**kwargs):
        return None

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 1

    async def _set_status(cid, status, **kwargs):
        statuses.append((cid, status))
        return {"id": cid}

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome == "not_generalizable"
    assert statuses == [(1, "not_generalizable")]
    assert indexed == []


async def test_duplicate_rule_is_not_indexed_twice():
    from bot.agent.kb_distill import process_candidate

    indexed, statuses = [], []

    async def _distill(**kwargs):
        return {"symptom": "s", "rule": "QR генерирует банк", "action": "a"}

    async def _similar(rule_text):
        return {"id": 5, "content": "Правило: QR генерирует банк", "score": 0.96}

    async def _overlap(new, existing, **kwargs):
        return ("duplicate", "то же самое")

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 1

    async def _set_status(cid, status, **kwargs):
        statuses.append((cid, status))
        return {"id": cid}

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _similar_fn=_similar,
        _overlap_fn=_overlap, _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome == "duplicate"
    assert indexed == [] and statuses == [(1, "duplicate")]


async def test_refining_rule_is_added_with_reference():
    from bot.agent.kb_distill import process_candidate

    indexed = []

    async def _distill(**kwargs):
        return {"symptom": "s", "rule": "QR на PAX генерирует Сбер", "action": "a"}

    async def _similar(rule_text):
        return {"id": 5, "content": "Правило: QR генерирует банк", "score": 0.91}

    async def _overlap(new, existing, **kwargs):
        return ("refines", "уточняет модель терминала")

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 8

    async def _set_status(cid, status, **kwargs):
        return {"id": cid}

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _similar_fn=_similar,
        _overlap_fn=_overlap, _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome == "auto_added"
    assert len(indexed) == 1


async def test_contradiction_goes_to_the_human():
    from bot.agent.kb_distill import process_candidate

    indexed, statuses = [], []

    async def _distill(**kwargs):
        return {"symptom": "s", "rule": "QR генерирует ОФД", "action": "a"}

    async def _similar(rule_text):
        return {"id": 5, "content": "Правило: QR генерирует банк", "score": 0.93}

    async def _overlap(new, existing, **kwargs):
        return ("contradicts", "адресат другой")

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 1

    async def _set_status(cid, status, **kwargs):
        statuses.append((cid, status, kwargs))
        return {"id": cid}

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _similar_fn=_similar,
        _overlap_fn=_overlap, _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome == "conflict"
    assert indexed == []                       # спорное правило в базу не идёт
    cid, status, extra = statuses[0]
    assert status == "conflict"
    assert extra["conflict_item_id"] == 5
    assert "ОФД" in extra["rule_json"]


async def test_already_decided_candidate_writes_nothing():
    """Гонка двух проходов не должна записать правило дважды: статус меняется
    ДО индексации, и проигравший проход видит, что pending-строки уже нет."""
    from bot.agent.kb_distill import process_candidate

    indexed = []

    async def _distill(**kwargs):
        return {"symptom": "s", "rule": "r", "action": "a"}

    async def _similar(rule_text):
        return None

    async def _index(**kwargs):
        indexed.append(kwargs)
        return 1

    async def _set_status(cid, status, **kwargs):
        return None                            # UPDATE не нашёл pending-строку

    outcome = await process_candidate(
        _candidate(), _distill_fn=_distill, _similar_fn=_similar,
        _index_fn=_index, _status_fn=_set_status,
    )
    assert outcome is None and indexed == []


# --- проход по очереди ------------------------------------------------------


async def test_process_pending_counts_outcomes():
    from bot.agent.kb_distill import process_pending_candidates

    async def _list(limit):
        return [_candidate(1), _candidate(2), _candidate(3)]

    outcomes = iter(["auto_added", "not_generalizable", "conflict"])

    async def _one(candidate, **kwargs):
        return next(outcomes)

    async def _sleep(_):
        return None

    stats = await process_pending_candidates(
        _list_fn=_list, _process_fn=_one, _sleep_fn=_sleep,
    )
    assert stats == {"auto_added": 1, "not_generalizable": 1, "conflict": 1,
                     "duplicate": 0, "errors": 0}


async def test_one_candidate_failure_does_not_stop_the_pass():
    from bot.agent.kb_distill import process_pending_candidates

    async def _list(limit):
        return [_candidate(1), _candidate(2)]

    async def _one(candidate, **kwargs):
        if candidate["id"] == 1:
            raise RuntimeError("429")
        return "auto_added"

    async def _sleep(_):
        return None

    stats = await process_pending_candidates(
        _list_fn=_list, _process_fn=_one, _sleep_fn=_sleep,
    )
    assert stats["errors"] == 1 and stats["auto_added"] == 1

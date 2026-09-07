"""Контекст, который у бота уже был, но до черновика не доезжал.

Описания вложений Vision писал в ticket_topics.photo_descriptions с апреля, и
читали их только автозаполнение полей и индексатор базы знаний. Черновик их не
видел — клиент присылал скриншот ошибки, бот спрашивал «какая ошибка на экране».
То же с полями тикета, которые бот сам же и заполняет, и с заметками после
звонка.
"""
from types import SimpleNamespace

import numpy as np

import bot.config as config_module
from bot.agent.context import build_agent_context


def _posts():
    return [
        SimpleNamespace(user_id=1, text="касса выдаёт ошибку", post_id=1),
        SimpleNamespace(user_id=1, text="вот скриншот", post_id=2),
    ]


async def _embed(text, task_type="query"):
    return np.ones(4, dtype=np.float32)


async def _no_similar(emb, *, limit=3, query_text="", company_id="", **kw):
    return []


async def _no_wiki(title):
    return None


async def _no_pattern(equipment, keywords):
    return None


async def _ctx(topic_fn, **over):
    kwargs = dict(
        _history_fn=lambda p, i: "H", _embed_fn=_embed, _similar_fn=_no_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=_no_wiki, _pattern_fn=_no_pattern,
        _topic_fn=topic_fn,
    )
    kwargs.update(over)
    return await build_agent_context(
        _posts(), SimpleNamespace(client_id=1), "Ошибка на кассе",
        ticket_id="T1", **kwargs,
    )


def _topic(**fields):
    base = dict(photo_descriptions="", call_notes="", env_option_id=None,
                company_name="", priority="", status="")
    base.update(fields)

    async def _fn(ticket_id):
        return SimpleNamespace(**base)

    return _fn


# --- D1: описания вложений -------------------------------------------------


async def test_photo_descriptions_reach_the_context():
    ctx = await _ctx(_topic(photo_descriptions="на экране кассы ошибка E103"))
    assert "E103" in ctx["attachments"]


async def test_photo_descriptions_are_not_grounding_evidence():
    """Описание картинки — факт о тикете, а не источник. В evidence его нет,
    иначе self-check начнёт требовать опоры на пересказ Vision."""
    ctx = await _ctx(_topic(photo_descriptions="ошибка E103"))
    assert all(e["source_type"] != "attachment" for e in ctx["evidence"])
    assert "E103" not in str(ctx["evidence"])


async def test_call_notes_reach_the_context():
    ctx = await _ctx(_topic(call_notes="в звонке: терминал от Сбера, ФН заполнен"))
    assert "Сбера" in ctx["call_notes"]


async def test_context_empty_when_topic_has_nothing():
    ctx = await _ctx(_topic())
    assert ctx["attachments"] == "" and ctx["call_notes"] == ""
    assert ctx["ticket_facts"] == ""


async def test_missing_topic_does_not_break_context():
    async def _none(ticket_id):
        return None

    ctx = await _ctx(_none)
    assert ctx["attachments"] == "" and ctx["ticket_facts"] == ""


async def test_topic_lookup_error_does_not_break_context():
    async def _boom(ticket_id):
        raise RuntimeError("база недоступна")

    ctx = await _ctx(_boom)
    assert ctx["attachments"] == ""


async def test_topic_not_queried_without_ticket_id():
    called = {"v": False}

    async def _spy(ticket_id):
        called["v"] = True
        return None

    await build_agent_context(
        _posts(), SimpleNamespace(client_id=1), "t",
        _history_fn=lambda p, i: "H", _embed_fn=_embed, _similar_fn=_no_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=_no_wiki, _pattern_fn=_no_pattern,
        _topic_fn=_spy,
    )
    assert called["v"] is False


# --- D2: поля тикета -------------------------------------------------------


async def test_ticket_facts_include_environment_name_not_id():
    """В промпт идёт «Posiflora Retail», а не «14»: id — деталь HDE, модель по
    нему ничего не поймёт."""
    from bot.ticket_fields import OKRUZHENIE_OPTIONS
    env_id = next(iter(OKRUZHENIE_OPTIONS))
    ctx = await _ctx(_topic(env_option_id=env_id))
    assert OKRUZHENIE_OPTIONS[env_id] in ctx["ticket_facts"]
    assert env_id not in ctx["ticket_facts"]


async def test_ticket_facts_include_company():
    ctx = await _ctx(_topic(company_name="Цветочная База"))
    assert "Цветочная База" in ctx["ticket_facts"]


async def test_unclassified_environment_is_omitted():
    """env_option_id == '' означает «классифицировали и не определили» — писать
    в промпт нечего, а «не определено» модель прочтёт как факт."""
    ctx = await _ctx(_topic(env_option_id=""))
    assert "окружение" not in ctx["ticket_facts"].lower()


async def test_unknown_environment_id_is_omitted():
    ctx = await _ctx(_topic(env_option_id="99999"))
    assert ctx["ticket_facts"] == ""


# --- D1/D2 в промпте -------------------------------------------------------


async def test_generate_puts_attachments_and_facts_into_prompt():
    from bot.agent.generate import generate_agent_draft
    seen = {}

    async def _call(history, *, system, model, **kwargs):
        seen["system"], seen["history"] = system, history
        return '{"action":"ANSWER","suit":"s","client":"c","memo":"m","confidence":80}'

    await generate_agent_draft(
        {"history": "H", "evidence": [], "demos": [],
         "attachments": "на экране ошибка E103",
         "call_notes": "в звонке: терминал Сбера",
         "ticket_facts": "Компания: Цветочная База"},
        "Ошибка",
        _call_fn=_call, _prompt_fn=lambda *a, **k: "BASE",
        _format_fn=_fake_format,
    )
    assert "E103" in seen["system"]
    assert "Сбера" in seen["system"]
    assert "Цветочная База" in seen["system"]


async def test_prompt_marks_attachment_text_as_a_description():
    """Описание даёт Vision, оно может врать. «По описанию вложения» вместо «на
    скриншоте видно» — чтобы модель не выдавала пересказ за наблюдение."""
    from bot.agent.generate import generate_agent_draft
    seen = {}

    async def _call(history, *, system, model, **kwargs):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"s","client":"c","memo":"m","confidence":80}'

    await generate_agent_draft(
        {"history": "H", "evidence": [], "demos": [], "attachments": "ошибка E103"},
        "t", _call_fn=_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=_fake_format,
    )
    assert "описани" in seen["system"].lower()


async def test_prompt_unchanged_without_ticket_context():
    from bot.agent.generate import generate_agent_draft
    seen = {}

    async def _call(history, *, system, model, **kwargs):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"s","client":"c","memo":"m","confidence":80}'

    await generate_agent_draft(
        {"history": "H", "evidence": [], "demos": []},
        "t", _call_fn=_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=_fake_format,
    )
    for label in ("вложени", "звонк", "Известно о тикете"):
        assert label not in seen["system"]


async def _fake_format():
    return "FORMAT"

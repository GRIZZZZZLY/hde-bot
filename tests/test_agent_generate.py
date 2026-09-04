import pytest
from bot.agent.generate import generate_agent_draft

_CTX = {
    "history": "Клиент: касса не печатает чек",
    "client_text": "касса не печатает чек",
    "equipment": "АТОЛ",
    "evidence": [
        {"source_type": "knowledge_item", "source_id": 12, "rank": 1,
         "score": 0.9, "title": None, "used_excerpt": "Помогла перезагрузка"},
    ],
    "retrieval_query": "q", "wiki": None, "solution_steps": None,
    "grounds": ["KB#12"], "confidence": 90,
}


@pytest.mark.asyncio
async def test_generate_agent_draft_passes_evidence_and_parses():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        seen["model"] = model
        seen["reasoning_effort"] = reasoning_effort
        return '{"action":"ANSWER","suit":"чек","client":"перезагрузите кассу","memo":"м","confidence":85}'

    def fake_prompt(title, rag_examples=None, wiki_context=None, *, equipment=None,
                    solution_steps=None, format_instructions=None):
        seen["rag"] = rag_examples
        return "SYSTEM"

    async def fake_format():
        return "FORMAT"

    draft = await generate_agent_draft(
        _CTX, "Не печатает чек",
        _call_fn=fake_call, _prompt_fn=fake_prompt, _format_fn=fake_format,
    )
    assert draft["action"] == "ANSWER"
    assert seen["rag"] == ["Помогла перезагрузка"]     # evidence → draft prompt
    assert "JSON" in seen["system"]
    from bot.config import config
    assert seen["model"] == config.agent_draft_model


@pytest.mark.asyncio
async def test_generate_agent_draft_none_on_garbage():
    async def bad_call(prompt, *, system=None, model=None, max_tokens=None,
                       temperature=None, reasoning_effort=""):
        return "мусор"

    async def fake_format():
        return "F"

    assert await generate_agent_draft(
        _CTX, "t", _call_fn=bad_call, _prompt_fn=lambda *a, **k: "S", _format_fn=fake_format,
    ) is None


_CTX_PAIRS = dict(_CTX)
# few-shot приходит из demos, не из evidence (инвариант I3)
_CTX_PAIRS["demos"] = [
    {"source_type": "dialogue_pair", "source_id": 5, "rank": 1, "score": 0.9,
     "title": "тикет T9",
     "used_excerpt": "Вопрос: похожий вопрос\nОтвет оператора: мой прошлый ответ"},
]


@pytest.mark.asyncio
async def test_fewshot_block_appended_to_system():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"с","client":"к","memo":"м","confidence":80}'

    async def fake_format():
        return "F"

    await generate_agent_draft(
        _CTX_PAIRS, "t",
        _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=fake_format,
    )
    assert "похожие обращения" in seen["system"]
    assert "мой прошлый ответ" in seen["system"]


@pytest.mark.asyncio
async def test_no_fewshot_block_without_pairs():
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"с","client":"к","memo":"м","confidence":80}'

    async def fake_format():
        return "F"

    await generate_agent_draft(
        _CTX, "t",
        _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=fake_format,
    )
    assert "похожие обращения" not in seen["system"]


@pytest.mark.asyncio
async def test_json_override_is_last_instruction_after_pair_examples():
    """Активный промпт из БД (v15) требует <reasoning> и текстовый формат —
    финальный JSON-override обязан идти ПОСЛЕДНИМ, после few-shot блока,
    иначе qwen отвечает текстом и драфт молча падает (прод-инцидент 2026-07-12)."""
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        return '{"action":"ANSWER","suit":"с","client":"к","memo":"м","confidence":80}'

    async def fake_format():
        return "FORMAT с <reasoning> инструкцией"

    ctx = dict(_CTX)
    # few-shot приходит из demos (инвариант I3 split-а)
    ctx["demos"] = [
        {"source_type": "dialogue_pair", "source_id": 1, "rank": 1,
         "score": 0.8, "title": None, "used_excerpt": "Клиент: х\nОператор: у"},
    ]
    await generate_agent_draft(
        ctx, "t",
        _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE", _format_fn=fake_format,
    )
    system = seen["system"]
    assert "ТОЛЬКО один JSON" in system
    assert system.rindex("ТОЛЬКО один JSON") > system.rindex("похожие обращения")
    assert "<reasoning>" in system.split("похожие обращения")[1]  # override после примеров


@pytest.mark.asyncio
async def test_parse_fail_logs_warning_with_raw_head(caplog):
    """Молчаливый откат на legacy невидим в логах — провал парса драфта
    обязан оставлять warning с началом сырого ответа модели."""
    import logging

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        return "<reasoning>размышления</reasoning>\nСуть: текст без JSON"

    async def fake_format():
        return "F"

    with caplog.at_level(logging.WARNING, logger="bot.agent.generate"):
        draft = await generate_agent_draft(
            _CTX, "t",
            _call_fn=fake_call, _prompt_fn=lambda *a, **k: "BASE",
            _format_fn=fake_format,
        )
    assert draft is None
    assert any("draft parse failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_draft_prompt_drops_reasoning_directive_but_keeps_style_rules():
    """Агентный путь отдаёт JSON, а формат-инструкции требуют блок <reasoning>.
    qwen выполнял инструкцию, JSON обрывался на max_tokens и драфт молча падал
    на legacy — 19 таких провалов в прод-логе за 2026-08-22..09-04."""
    seen = {}

    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        seen["system"] = system
        seen["max_tokens"] = max_tokens
        return '{"action":"ANSWER","suit":"s","client":"c","memo":"м","confidence":80}'

    def fake_prompt(title, rag_examples=None, wiki_context=None, *, equipment=None,
                    solution_steps=None, format_instructions=None):
        seen["format_instructions"] = format_instructions
        return f"SYSTEM\n{format_instructions}"

    async def fake_format():
        return (
            "Перед ответом заполни блок рассуждения (скрыт от пользователя):\n"
            "<reasoning>\n1. Что сломано?\n</reasoning>\n\n"
            "Клиенту: <одно предложение, императив, максимум 20 слов>\n"
            "Запретные фразы: «дайте знать».\n"
        )

    draft = await generate_agent_draft(
        _CTX, "t", _call_fn=fake_call, _prompt_fn=fake_prompt, _format_fn=fake_format,
    )
    assert draft is not None
    assert "<reasoning>" not in seen["format_instructions"]
    assert "императив" in seen["format_instructions"]     # правила стиля живы
    assert "Запретные фразы" in seen["format_instructions"]
    assert seen["max_tokens"] >= 1200                     # JSON помещается целиком


@pytest.mark.asyncio
async def test_draft_survives_model_emitting_reasoning_block():
    """Даже если модель всё равно напишет рассуждение, JSON из него достаётся."""
    async def fake_call(prompt, *, system=None, model=None, max_tokens=None,
                        temperature=None, reasoning_effort=""):
        return (
            "<reasoning>\nСломан {принтер} или {драйвер}\n</reasoning>\n"
            '{"action":"ASK","suit":"s","client":"Какая модель кассы?",'
            '"memo":"—","confidence":40}'
        )

    def fake_prompt(title, rag_examples=None, wiki_context=None, *, equipment=None,
                    solution_steps=None, format_instructions=None):
        return "SYSTEM"

    async def fake_format():
        return "FORMAT"

    draft = await generate_agent_draft(
        _CTX, "t", _call_fn=fake_call, _prompt_fn=fake_prompt, _format_fn=fake_format,
    )
    assert draft is not None and draft["action"] == "ASK"
    assert draft["client"] == "Какая модель кассы?"

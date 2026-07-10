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

import json

from bot.agent.selfcheck import self_check

_EVIDENCE = [
    {"source_type": "knowledge_item", "source_id": 12, "rank": 1, "score": 0.9,
     "title": None, "used_excerpt": "Клиенту помогла перезагрузка кассы после замены бумаги"},
]


async def test_self_check_prompt_contains_evidence_content():
    seen = {}

    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        seen["user"] = user
        seen["model"] = model
        return json.dumps({"status": "supported", "fallback_action": "ASK",
                           "fallback_client_text": ""})

    r = await self_check("вопрос", "перезагрузите кассу", _EVIDENCE, "история",
                         _call_fn=fake_call)
    assert r["status"] == "supported"
    assert "помогла перезагрузка кассы" in seen["user"]   # КОНТЕНТ, не метка
    from bot.config import config
    assert seen["model"] == config.agent_selfcheck_model


async def test_self_check_unsupported_fallback():
    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return json.dumps({"status": "unsupported", "fallback_action": "ESCALATE",
                           "fallback_client_text": ""})

    r = await self_check("в", "выдумка", [], "и", _call_fn=fake_call)
    assert r["status"] == "unsupported"
    assert r["fallback_action"] == "ESCALATE"


async def test_self_check_prompt_forbids_reasking_known_facts():
    """Fallback-вопрос не должен переспрашивать то, что клиент уже сообщил."""
    seen = {}

    async def fake_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        seen["system"] = system
        return json.dumps({"status": "supported", "fallback_action": "ASK",
                           "fallback_client_text": ""})

    await self_check("вопрос", "ответ", _EVIDENCE, "история", _call_fn=fake_call)
    assert "уже сообщил" in seen["system"]


async def test_self_check_conservative_default():
    async def bad_call(system, user, *, model=None, temperature=0.0, max_tokens=600):
        return "не json"

    r = await self_check("в", "о", [], "и", _call_fn=bad_call)
    assert r == {"status": "unsupported", "fallback_action": "ASK",
                 "fallback_client_text": ""}

"""Tests for the qwen/reasoning-model switch: env-driven summary model,
reasoning_effort passthrough, <think> stripping."""
import bot.ai_summary as ai
import bot.config as config_module


def test_config_summary_model_and_reasoning_defaults():
    fresh = config_module.Config.from_env()
    assert fresh.groq_summary_model == "llama-3.3-70b-versatile"
    assert fresh.groq_reasoning_effort == ""


def test_config_summary_model_and_reasoning_env(monkeypatch):
    monkeypatch.setenv("GROQ_SUMMARY_MODEL", "qwen/qwen3.6-27b")
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "none")
    fresh = config_module.Config.from_env()
    assert fresh.groq_summary_model == "qwen/qwen3.6-27b"
    assert fresh.groq_reasoning_effort == "none"


def test_strip_reasoning_removes_think_block():
    text = "<think>\nThinking process...\nразмышления\n</think>\nПроверьте бумагу."
    assert ai._strip_reasoning(text) == "Проверьте бумагу."


def test_strip_reasoning_still_removes_reasoning_tag():
    assert ai._strip_reasoning("<reasoning>x</reasoning>Ответ") == "Ответ"


class _FakeResp:
    def __init__(self, payload_sink):
        self._sink = payload_sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    status = 200

    async def json(self):
        return {"choices": [{"message": {"content": "ОК"}}]}


class _FakeSession:
    def __init__(self, payload_sink):
        self._sink = payload_sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, json=None, headers=None, timeout=None):
        self._sink["payload"] = json
        return _FakeResp(self._sink)


async def test_call_groq_text_includes_reasoning_effort_when_set(monkeypatch):
    sink = {}
    monkeypatch.setattr(ai.config, "groq_api_key", "k")
    monkeypatch.setattr(ai, "shared_session", lambda: _FakeSession(sink))
    await ai.call_groq_text("hi", model="qwen/qwen3.6-27b", reasoning_effort="none")
    assert sink["payload"]["reasoning_effort"] == "none"
    assert sink["payload"]["model"] == "qwen/qwen3.6-27b"


async def test_call_groq_text_omits_reasoning_effort_when_empty(monkeypatch):
    sink = {}
    monkeypatch.setattr(ai.config, "groq_api_key", "k")
    monkeypatch.setattr(ai, "shared_session", lambda: _FakeSession(sink))
    await ai.call_groq_text("hi", model="llama-3.3-70b-versatile")
    assert "reasoning_effort" not in sink["payload"]


def test_eval_payload_includes_reasoning_effort_when_set():
    from bot.optimizer.evaluator import _eval_payload
    p = _eval_payload("qwen/qwen3.6-27b", "sys", "usr", "none")
    assert p["model"] == "qwen/qwen3.6-27b"
    assert p["reasoning_effort"] == "none"


def test_eval_payload_omits_reasoning_effort_when_empty():
    from bot.optimizer.evaluator import _eval_payload
    p = _eval_payload("llama-3.3-70b-versatile", "sys", "usr", "")
    assert "reasoning_effort" not in p

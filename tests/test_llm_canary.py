"""Канарейка моделей на старте + отсутствие зашитых model-id.

Инцидент 2026-08-18: Groq снял llama-3.3-70b и llama-4-scout, зашитые в модули
id упали в 404 на каждом вызове, а увидели это по логу тикетов. Здесь
проверяется, что id берутся из env и что снятая модель обнаруживается на старте.
"""
import pytest

import bot.config as config_module
from bot import llm_canary
from bot.llm_canary import (
    GROQ,
    OPENROUTER,
    check_models,
    format_report,
    model_roles,
    report_dead_models,
)


@pytest.fixture(autouse=True)
def canary_config(monkeypatch):
    cfg = config_module.config
    monkeypatch.setattr(cfg, "groq_api_key", "k")
    monkeypatch.setattr(cfg, "openrouter_api_key", "")
    monkeypatch.setattr(cfg, "groq_summary_model", "qwen/q3")
    monkeypatch.setattr(cfg, "agent_draft_model", "qwen/q3")
    monkeypatch.setattr(cfg, "agent_selfcheck_model", "oss/120")
    monkeypatch.setattr(cfg, "groq_vision_model", "qwen/q3")
    monkeypatch.setattr(cfg, "groq_classify_fallback_model", "oss/20")
    monkeypatch.setattr(cfg, "optimizer_judge_model", "oss/120")
    monkeypatch.setattr(cfg, "optimizer_mutation_models", ("oss/120", "llama/8b"))
    monkeypatch.setattr(cfg, "openrouter_model", "gemma/free")
    monkeypatch.setattr(cfg, "llm_canary_enabled", True)
    return cfg


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


# --- карта ролей ---

def test_roles_dedup_shared_model():
    """Один id в трёх ролях — одна проверка, но все роли в отчёте."""
    roles = model_roles()
    assert roles[(GROQ, "qwen/q3")] == ["суммарка", "черновик агента", "картинки"]
    assert roles[(GROQ, "oss/120")] == [
        "self-check и судья пар", "судья оптимизатора", "мутации промпта",
    ]
    assert (OPENROUTER, "gemma/free") not in roles  # ключа OpenRouter нет


def test_roles_include_openrouter_when_key_present(monkeypatch):
    monkeypatch.setattr(config_module.config, "openrouter_api_key", "or-key")
    assert (OPENROUTER, "gemma/free") in model_roles()


def test_roles_skip_empty_model(monkeypatch):
    monkeypatch.setattr(config_module.config, "groq_vision_model", "")
    assert all(model for _, model in model_roles())


# --- проверка моделей ---

@pytest.mark.asyncio
async def test_check_models_reports_only_dead():
    async def fake_probe(provider, model):
        return "HTTP 404: model_not_found" if model == "oss/20" else None

    dead = await check_models(_probe_fn=fake_probe)

    assert list(dead) == [(GROQ, "oss/20")]
    assert "404" in dead[(GROQ, "oss/20")]


@pytest.mark.asyncio
async def test_probe_treats_429_as_alive(monkeypatch):
    """Исчерпанная квота — не смерть модели: free-tier упирается в TPM штатно."""
    calls = []

    class _Resp:
        status = 429

        async def text(self):
            return "rate limit"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **kw):
            calls.append(kw.get("json", {}).get("model"))
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(llm_canary.aiohttp, "ClientSession", lambda *a, **kw: _Session())

    assert await llm_canary.probe_model(GROQ, "qwen/q3") is None
    assert calls == ["qwen/q3"]


@pytest.mark.asyncio
async def test_probe_returns_error_on_404(monkeypatch):
    class _Resp:
        status = 404

        async def text(self):
            return '{"error":{"code":"model_not_found"}}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **kw):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(llm_canary.aiohttp, "ClientSession", lambda *a, **kw: _Session())

    error = await llm_canary.probe_model(GROQ, "dead/model")
    assert error is not None and "404" in error and "model_not_found" in error


@pytest.mark.asyncio
async def test_probe_without_key_is_silent(monkeypatch):
    monkeypatch.setattr(config_module.config, "groq_api_key", "")
    assert await llm_canary.probe_model(GROQ, "qwen/q3") is None


# --- доклад оператору ---

@pytest.mark.asyncio
async def test_report_notifies_with_roles_and_env_var():
    async def fake_check(roles):
        return {(GROQ, "oss/20"): "HTTP 404: model_not_found"}

    bot = _FakeBot()
    dead = await report_dead_models(bot, _check_fn=fake_check)

    assert dead
    assert len(bot.sent) == 1
    text = bot.sent[0]
    assert "oss/20" in text
    assert "фолбэк классификаторов" in text
    assert "GROQ_CLASSIFY_FALLBACK_MODEL" in text  # чем починить, без чтения кода


@pytest.mark.asyncio
async def test_report_silent_when_all_alive():
    async def fake_check(roles):
        return {}

    bot = _FakeBot()
    assert await report_dead_models(bot, _check_fn=fake_check) == {}
    assert bot.sent == []


@pytest.mark.asyncio
async def test_report_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(config_module.config, "llm_canary_enabled", False)

    async def fake_check(roles):  # pragma: no cover — не должен вызываться
        raise AssertionError("canary must not probe when disabled")

    bot = _FakeBot()
    assert await report_dead_models(bot, _check_fn=fake_check) == {}


@pytest.mark.asyncio
async def test_report_survives_telegram_failure():
    """Не смогли доложить — не роняем старт бота."""
    async def fake_check(roles):
        return {(GROQ, "oss/20"): "HTTP 404"}

    class _BrokenBot:
        async def send_message(self, *a, **kw):
            raise RuntimeError("telegram down")

    dead = await report_dead_models(_BrokenBot(), _check_fn=fake_check)
    assert dead  # вердикт вернулся, исключение не улетело


def test_format_report_lists_every_dead_model():
    roles = {(GROQ, "a/1"): ["суммарка"], (GROQ, "b/2"): ["картинки"]}
    text = format_report({(GROQ, "a/1"): "HTTP 404", (GROQ, "b/2"): "HTTP 401"}, roles)
    assert "a/1" in text and "b/2" in text
    assert "GROQ_SUMMARY_MODEL" in text and "GROQ_VISION_MODEL" in text


# --- ни одного зашитого id в коде ---

def test_no_hardcoded_model_ids_in_modules():
    """Литерал model-id в модуле = все вызовы падают одновременно и правятся релизом."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "bot"
    # Требуем "/" или "-" внутри литерала: так отсекаются метки счётчиков
    # ("gemma4", "scout") и остаются настоящие id вида openai/gpt-oss-120b.
    pattern = re.compile(
        r'"(?=[^"]*[/-])[a-z0-9/._:-]*(gpt-oss|qwen|llama|gemma|gemini|mixtral)[a-z0-9/._:-]*"'
    )
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "config.py":  # дефолты живут здесь по определению
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "embedding" not in line.lower():
                offenders.append(f"{path.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "зашитые model-id:\n" + "\n".join(offenders)

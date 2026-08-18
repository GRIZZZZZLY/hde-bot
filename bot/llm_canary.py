"""Канарейка моделей: на старте спрашиваем провайдера, живы ли настроенные id.

Провайдер снимает модели без предупреждения — 2026-08-18 из обслуживания ушли
llama-3.3-70b-versatile и llama-4-scout, и каждый вызов начал отдавать 404
model_not_found. Все id теперь в env (см. config), но опечатка или снятая модель
всё равно видны только когда упадёт первый живой запрос: в проде это тикет
клиента, в оптимизаторе — весь ночной прогон. Канарейка задаёт по одному
самому дешёвому вопросу на КАЖДЫЙ уникальный id и докладывает оператору, какие
роли остались без модели — вместе с именем переменной, которой это правится.

Два решения, без которых канарейка вредна:

1. **429 — не смерть.** Free-tier легко упирается в TPM; исчерпанная квота
   означает, что модель ЖИВА. Иначе бот при каждом старте кричал бы «модель
   недоступна» на здоровой конфигурации.
2. **Старт не блокируется.** Бот без суммарки полезнее выключенного бота, а
   недоступный провайдер — не причина не обслуживать тикеты.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from .config import config
from .llm_semaphore import LLM_SEMAPHORE

logger = logging.getLogger(__name__)

GROQ = "groq"
OPENROUTER = "openrouter"

_URLS = {
    GROQ: "https://api.groq.com/openai/v1/chat/completions",
    OPENROUTER: "https://openrouter.ai/api/v1/chat/completions",
}

# Переменная окружения, которой правится роль — попадает прямо в сообщение
# оператору, чтобы починка не требовала чтения кода.
_ROLE_ENV = {
    "суммарка": "GROQ_SUMMARY_MODEL",
    "черновик агента": "AGENT_DRAFT_MODEL",
    "self-check и судья пар": "AGENT_SELFCHECK_MODEL",
    "картинки": "GROQ_VISION_MODEL",
    "фолбэк классификаторов": "GROQ_CLASSIFY_FALLBACK_MODEL",
    "судья оптимизатора": "OPTIMIZER_JUDGE_MODEL",
    "мутации промпта": "OPTIMIZER_MUTATION_MODELS",
    "фолбэк мутаций (OpenRouter)": "OPENROUTER_MODEL",
}

_TIMEOUT = 15.0


def model_roles() -> dict[tuple[str, str], list[str]]:
    """(провайдер, модель) → роли. Один id в нескольких ролях = одна проверка."""
    pairs: list[tuple[str, str, str]] = [
        (GROQ, config.groq_summary_model, "суммарка"),
        (GROQ, config.agent_draft_model, "черновик агента"),
        (GROQ, config.agent_selfcheck_model, "self-check и судья пар"),
        (GROQ, config.groq_vision_model, "картинки"),
        (GROQ, config.groq_classify_fallback_model, "фолбэк классификаторов"),
        (GROQ, config.optimizer_judge_model, "судья оптимизатора"),
    ]
    pairs += [
        (GROQ, model, "мутации промпта")
        for model in config.optimizer_mutation_models
    ]
    if config.openrouter_api_key:
        pairs.append(
            (OPENROUTER, config.openrouter_model, "фолбэк мутаций (OpenRouter)")
        )

    roles: dict[tuple[str, str], list[str]] = {}
    for provider, model, role in pairs:
        if not model:
            continue
        roles.setdefault((provider, model), [])
        if role not in roles[(provider, model)]:
            roles[(provider, model)].append(role)
    return roles


async def probe_model(provider: str, model: str) -> str | None:
    """None — модель ответила (или проверять нечем). Иначе текст ошибки.

    Самый дешёвый вопрос: один токен на выходе. 404 model_not_found и 401
    приходят на этом же запросе; 429 не считается провалом (см. модульный
    докстринг). Сетевой сбой тоже не провал модели — о нём только лог.
    """
    key = config.groq_api_key if provider == GROQ else config.openrouter_api_key
    if not key:
        return None  # без ключа проверять нечего — молчим, а не паникуем
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
            async with session.post(
                _URLS[provider],
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status in (200, 429):
                    return None
                body = await resp.text()
                return f"HTTP {resp.status}: {body[:120]}"
    except Exception as exc:
        logger.warning("canary: probe %s/%s failed: %s", provider, model, exc)
        return None


async def check_models(
    roles: dict[tuple[str, str], list[str]] | None = None, *, _probe_fn=None
) -> dict[tuple[str, str], str]:
    """Проверяет каждый уникальный id. Возвращает только мёртвые: ключ → ошибка."""
    probe = _probe_fn or probe_model
    targets = list((roles if roles is not None else model_roles()).keys())
    if not targets:
        return {}
    results = await asyncio.gather(
        *(probe(provider, model) for provider, model in targets)
    )
    return {
        target: error
        for target, error in zip(targets, results)
        if error is not None
    }


def format_report(
    dead: dict[tuple[str, str], str], roles: dict[tuple[str, str], list[str]]
) -> str:
    lines = ["🚨 <b>Модель недоступна у провайдера</b>", ""]
    for (provider, model), error in dead.items():
        role_names = roles.get((provider, model), [])
        envs = sorted({_ROLE_ENV[r] for r in role_names if r in _ROLE_ENV})
        lines.append(f"<code>{model}</code> ({provider})")
        lines.append(f"роли: {', '.join(role_names) or '—'}")
        lines.append(f"{error}")
        if envs:
            lines.append(f"правится в .env: {', '.join(envs)}")
        lines.append("")
    lines.append("Смена модели — правка .env и рестарт, без релиза.")
    return "\n".join(lines).strip()


async def report_dead_models(bot, *, _check_fn=None) -> dict[tuple[str, str], str]:
    """Проверка + доклад оператору. Возвращает мёртвые модели (пусто = всё живо)."""
    if not config.llm_canary_enabled:
        return {}
    roles = model_roles()
    check = _check_fn or check_models
    dead = await check(roles)
    if not dead:
        logger.info("LLM canary: %d model(s) alive", len(roles))
        return {}
    for (provider, model), error in dead.items():
        logger.error(
            "LLM canary: %s/%s unavailable (%s) — roles: %s",
            provider, model, error, ", ".join(roles.get((provider, model), [])),
        )
    try:
        await bot.send_message(
            config.personal_chat_id, format_report(dead, roles), parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("LLM canary: could not notify operator: %s", exc)
    return dead

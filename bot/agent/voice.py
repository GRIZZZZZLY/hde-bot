"""Системный промпт агента v2 (spec 2026-09-27 §4).

Порядок: постоянное (роль, голос, правила, примеры) → данные тикета → формат.
Голос живёт в bot/prompts/voice_ru.md и больше нигде: там же его читает судья.
Бюджет жёсткий — у qwen3.8 на Groq 8000 TPM, а запрос считается как промпт +
max_tokens. Динамические блоки режутся здесь, а не у вызывающего.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"

SYSTEM_BUDGET_CHARS = 9000
ALLOWED_URLS = ("https://anydesk.com/ru", "https://rudesktop.ru/downloads/")

_FACTS_LIMIT = 300
_SIDE_LIMIT = 250          # описание вложений и заметки звонка — каждое
_SOURCE_LIMIT = 600        # один кусок статьи
_DEMO_LIMIT = 250          # один прошлый ответ

_ROLE = (
    "Ты помогаешь инженеру 2-й линии поддержки: кассы (АТОЛ, Эвотор, Штрих-М, Viki), "
    "Posiflora, эквайринг. Пишешь черновик ответа клиенту, оператор отправит его сам. "
    "Быстрый шаг, который клиент сделает сам, лучше удалёнки."
)

_ACTION_RULES = (
    "ВЫБОР ДЕЙСТВИЯ (поле action):\n"
    "- ANSWER — есть шаг из источников, истории или типовых шагов: дай его;\n"
    "- ASK — не хватает данных: один вопрос и зачем он нужен;\n"
    "- ESCALATE — только деньги, фискальные изменения, необратимые действия, доступы "
    "(client пустой). «Передать специалисту» — не повод: оператор и есть специалист;\n"
    "- NO_ACTION — только если проблема решена или клиент благодарит после решения "
    "(client пустой). «Хорошо», «Жду», «Спасибо», телефон при нерешённой проблеме — не повод "
    "молчать: дай следующий шаг.\n"
    "Опоры нет нигде — ASK, шаг не выдумывай. Удалёнка — только если типовой шаг уже "
    "пробовали или его нет: AnyDesk https://anydesk.com/ru, RuDesktop "
    "https://rudesktop.ru/downloads/; какая программа у клиента, не знаешь — спроси.\n"
    "source_ids — метки источников ([KB#…], [пара#…]), на которые опирается шаг."
)

# Итоги проверки v2 (п.10.4.2): этими шагами операторы решали тикеты, где бот
# просил удалёнку или молчал; в базе знаний таких шагов нет.
_FIRST_STEPS = (
    "ТИПОВЫЕ ПЕРВЫЕ ШАГИ (если в источниках нет своего и в истории ещё не делали):\n"
    "- не печатает, «порт недоступен» → кабель кассы в другой USB-разъём, перезагрузить кассу;\n"
    "- касса по Wi-Fi не отвечает, inout error → перезагрузить роутер и кассу;\n"
    "- терминал не отвечает → перезагрузить роутер и терминал, повторить оплату;\n"
    "- Posiflora зависла, ошибка при заказе → закрыть и снова открыть Posiflora;\n"
    "- пропал Posiflora Service на Эвоторе → проверить подписку в личном кабинете Эвотора;\n"
    "- чеки не уходят в налоговую → проверить тариф в личном кабинете ОФД;\n"
    "- этикетки со смещением → прислать фото, как установлена лента.\n"
    "Не помогло — удалёнка."
)

_SUIT_MEMO_RULES = (
    "СУТЬ И ПАМЯТКА (для оператора, не для клиента):\n"
    "- suit — диагноз в 8–18 слов: что сломано и у какого ПО или оборудования, "
    "как заметка коллеге в чат;\n"
    "- memo — только конкретика через « • »: модель, путь в меню, ссылка из источников, "
    "что уже пробовали и результат, куда звонить; нечего сказать — «—». Пароли не пиши."
)

_FORMAT = (
    "ФОРМАТ: верни ТОЛЬКО один JSON-объект, без текста до и после:\n"
    '{"analysis": "что сломано, что уже пробовали, хватает ли данных — 1–2 предложения", '
    '"action": "ANSWER|ASK|ESCALATE|NO_ACTION", "suit": "...", "client": "...", '
    '"memo": "...", "source_ids": ["KB#1"], "confidence": 0}'
)


@lru_cache(maxsize=1)
def load_voice() -> str:
    return (_PROMPTS / "voice_ru.md").read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _examples_block() -> str:
    data = json.loads((_PROMPTS / "voice_examples.json").read_text(encoding="utf-8"))
    lines = ["ЭТАЛОННЫЕ ПРИМЕРЫ поля client:"]
    for ex in data.get("examples", []):
        lines.append(f"Ситуация: {ex['situation']}\nОтвет: {ex['client']}")
    return "\n\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _ticket_block(context: dict, ticket_title: str) -> str:
    parts = [f"Тема обращения: «{_clip(ticket_title, 150)}»"]
    facts = _clip(context.get("ticket_facts", ""), _FACTS_LIMIT)
    if facts:
        parts.append(f"Известно о тикете:\n{facts}")
    attachments = _clip(context.get("attachments", ""), _SIDE_LIMIT)
    if attachments:
        parts.append(
            "Описание вложений клиента (автоматическое, может быть неточным — "
            f"ссылайся «по описанию»):\n{attachments}"
        )
    call_notes = _clip(context.get("call_notes", ""), _SIDE_LIMIT)
    if call_notes:
        parts.append(f"Из звонка по тикету (клиенту уже известно, не переспрашивай):\n{call_notes}")

    sources = [
        f"[KB#{e.get('source_id')}] {_clip(e.get('used_excerpt', ''), _SOURCE_LIMIT)}"
        for e in context.get("evidence", [])
        if e.get("source_type") == "knowledge_item"
    ][:2]
    parts.append("Источники:\n" + ("\n\n".join(sources) if sources else "нет подходящих"))

    demos = [
        f"[пара#{d.get('source_id')}] {_clip(d.get('used_excerpt', ''), _DEMO_LIMIT)}"
        for d in context.get("demos", [])
    ][:2]
    if demos:
        parts.append(
            "Похожие прошлые ответы операторов (бери из них конкретику, форму — из «Голоса»):\n"
            + "\n\n".join(demos)
        )

    signals = []
    if context.get("stress"):
        signals.append("Сигнал: стресс")
    signals.append("Первый ответ: да" if context.get("first_staff_reply") else "Первый ответ: нет")
    parts.append("\n".join(signals))
    return "\n\n".join(parts)


def build_prompt(context: dict, ticket_title: str) -> str:
    """Системный промпт v2. История тикета уходит отдельным user-сообщением."""
    return "\n\n".join([
        _ROLE,
        load_voice(),
        _ACTION_RULES,
        _FIRST_STEPS,
        _SUIT_MEMO_RULES,
        _examples_block(),
        _ticket_block(context or {}, ticket_title),
        _FORMAT,
    ])

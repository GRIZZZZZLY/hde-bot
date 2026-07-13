"""Чистые помощники агентного пайплайна: инструкция выбора действия,
парсинг ответа модели, извлечение вопроса клиента, сборка Памятки."""
from __future__ import annotations

import html as _html
import json
import re as _re

AGENT_ACTIONS = ("ANSWER", "ASK", "ESCALATE", "NO_ACTION")


def build_action_instruction() -> str:
    return (
        "\n\nВыбери ОДНО действие:\n"
        "- ANSWER — данных достаточно, дай готовое к отправке решение;\n"
        "- ASK — данных не хватает, задай минимальный набор уточняющих вопросов "
        "одним сообщением (не более 4; если вопросы зависят друг от друга — только первый);\n"
        "- ESCALATE — вопрос нельзя решать без оператора (деньги, фискальные "
        "параметры, необратимые действия, доступы);\n"
        "- NO_ACTION — клиент не задал вопрос, ответ не требуется.\n"
        "Верни СТРОГО JSON без пояснений:\n"
        '{"action": "...", "suit": "краткая суть обращения", '
        '"client": "ответ клиенту: одно предложение ≤20 слов, императив, '
        'без приветствий и вежливых штампов (пусто для NO_ACTION/ESCALATE)", '
        '"memo": "шпаргалка оператору — только конкретика", "confidence": 0-100, '
        '"confidence_reason": "почему такая уверенность"}'
    )


def build_json_override() -> str:
    """Финальный JSON-override — обязан идти ПОСЛЕДНИМ в system-промпте, после
    few-shot. Прод-инцидент 2026-07-12: активный промпт v15 требует <reasoning>
    и текстовый формат Суть/Клиенту/Памятка, few-shot усиливают текстовый вывод —
    без финального override qwen отвечает текстом и драфт молча падает на legacy.
    ВАЖНО: override меняет только ОБЁРТКУ вывода (JSON вместо меток/reasoning);
    правила стиля и длины полей выше остаются обязательными для значений JSON
    (иначе «Клиенту» скатывается в душный дефолт — регрессия хотфикса 2026-07-12)."""
    return (
        "\n\nИТОГ — про ФОРМАТ ВЫВОДА: верни ТОЛЬКО один JSON-объект по схеме действия — "
        "без блока <reasoning>, без меток Суть/Клиенту/Памятка, без текста до или после JSON.\n"
        "Правила стиля и длины полей выше ОБЯЗАТЕЛЬНЫ для значений JSON: "
        '"client" — одно предложение ≤20 слов, императив, без приветствий, прощаний и '
        "штампов («дайте знать», «если есть вопросы», «в ближайшее время», «уточнить детали», "
        "«чтобы мы могли помочь»); для уточнения — ОДИН прямой вопрос по сути, без обёрток "
        "«пожалуйста, уточните, требуется ли…»; "
        '"memo" — только конкретика (модель/ПО, путь в меню, ссылка, куда звонить), иначе «—».'
    )


def parse_agent_draft(raw: str) -> dict | None:
    """Парсит JSON-ответ модели (терпим к ```json ограждениям)."""
    if not raw:
        return None
    match = _re.search(r"\{.*\}", raw.strip(), _re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("action") not in AGENT_ACTIONS:
        return None
    for key in ("suit", "client", "memo"):
        if not isinstance(obj.get(key, ""), str):
            return None
    try:
        conf = int(obj.get("confidence", 50))
    except (TypeError, ValueError):
        conf = 50
    return {
        "action": obj["action"],
        "suit": obj.get("suit", "").strip(),
        "client": obj.get("client", "").strip(),
        "memo": obj.get("memo", "").strip(),
        "confidence": max(0, min(100, conf)),
        "confidence_reason": str(obj.get("confidence_reason", "")).strip(),
    }


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def extract_client_text(posts, client_id) -> str:
    """Текст последнего сообщения клиента (по client_id), без HTML."""
    for post in reversed(list(posts)):
        if str(getattr(post, "user_id", "")) == str(client_id):
            text = _strip_html(getattr(post, "text", ""))
            if text:
                return text
    return ""


def compose_memo(base_memo: str, *, stale_warning: bool = False) -> str:
    """Памятка оператору — только тело от модели + предупреждение о свежести.

    Служебные строки (Действие·уверенность·self-check, «Основания: KB#…»,
    «Не хватает: …») убраны: они шумели в памятке и не нужны для решения тикета.
    Провенанс источников (grounds/confidence/self-check) сохраняется в
    БД-трассировке ai_suggestions, поэтому здесь их дублировать незачем."""
    lines = [base_memo.strip()] if base_memo.strip() else []
    if stale_warning:
        lines.append("⚠️ Пока готовился ответ, клиент прислал новое сообщение — проверь актуальность.")
    return "\n".join(lines)

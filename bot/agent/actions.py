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
        # Сверка 2026-09: операторы штатно начинают с удалёнки, а прежний
        # запрет «не первым шагом» гнал бота в эскалацию и расходился с
        # эталоном. Запрещаем не удалёнку, а её небрежную формулировку.
        "СНАЧАЛА посмотри во фрагменты источников. Если там есть конкретный шаг "
        "— порт, версия, путь в меню, кабель, «Статья: <url>» — дай клиенту ЕГО. "
        "Удалённый доступ предлагай, ТОЛЬКО когда конкретного шага в источниках "
        "нет и без подключения диагноз невозможен.\n"
        "Когда удалёнка всё же нужна, программу не угадывай: если в переписке не названа ни "
        "AnyDesk, ни RuDesktop — спроси, какая установлена. Если названа — дай "
        "ссылку и скажи, что прислать в ответ: RuDesktop "
        "https://rudesktop.ru/downloads/ — номер рабочего места и пароль; "
        "AnyDesk https://anydesk.com/ru — номер рабочего места.\n"
        "Пиши только то, что делает КЛИЕНТ. Первое лицо о собственных действиях "
        "как о свершившихся запрещено («Подключаюсь к вам», «Удалённо "
        "подключился», «Настроил вам», «Обновил драйвер») — оператор ещё ничего "
        "не сделал, и клиент будет ждать несуществующего подключения.\n"
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
        "«пожалуйста, уточните, требуется ли…»; в «client» нет первого лица о своих "
        "действиях («Подключаюсь», «Подключился», «Настроил») — только то, что делает клиент; "
        '"memo" — только конкретика (модель/ПО, путь в меню, ссылка, куда звонить), иначе «—».'
    )


# Формат-инструкции требуют блок <reasoning> перед ответом — на текстовом пути
# это полезно, на агентном несовместимо с JSON: qwen честно выполняет
# инструкцию, рассуждение съедает max_tokens, JSON не дописывается,
# parse_agent_draft возвращает None и агент молча падает на legacy. В прод-логе
# 2026-08-22..09-04 это 19 провалов парсинга с головой «<reasoning>».
_REASONING_LEADIN_RE = _re.compile(
    r"\n?[^\n]*(?:рассужден|reasoning)[^\n]*:[ \t]*\n<reasoning>.*?</reasoning>[ \t]*\n*",
    _re.DOTALL | _re.IGNORECASE,
)
_REASONING_SPAN_RE = _re.compile(r"\n?<reasoning>.*?</reasoning>[ \t]*\n*", _re.DOTALL)


def strip_reasoning_directive(instructions: str) -> str:
    """Убрать требование блока <reasoning> из формат-инструкций (агентный путь).

    Инструкции приходят либо из БД (активная версия), либо из встроенной
    константы, поэтому вырезаем по тексту, а не по флагу. Если блока нет —
    строка возвращается как есть.
    """
    if not instructions or "<reasoning>" not in instructions:
        return instructions
    text = _REASONING_LEADIN_RE.sub("\n", instructions, count=1)
    if "<reasoning>" in text:
        text = _REASONING_SPAN_RE.sub("\n", text)
    return text


def compose_selfcheck_warning(check: dict, base_memo: str) -> str:
    """Пометка оператору вместо подмены драфта fallback-текстом self-check.

    Сверка 2026-09 (тикеты 197159, 199872, 197210): драфт совпадал с тем, что
    написал оператор, self-check возвращал unsupported, и клиенту предлагался
    его fallback — «передадим профильному специалисту». Драфт терялся, а
    расхождение уходило в сверку как «эскалация вместо решения». Клиенту ничего
    не уходит без кнопки оператора, поэтому предупреждение полезнее подмены.
    """
    if not check.get("checked", True):
        head = "⚠️ Self-check не отработал — факты не проверены."
    else:
        head = "⚠️ Ответ без опоры на источники — проверь факты перед отправкой."
    hint = (check.get("fallback_client_text") or "").strip()
    if hint:
        head += f" Запасной вариант: {hint}"
    return f"{head} {base_memo}".strip()


def parse_agent_draft(raw: str) -> dict | None:
    """Парсит JSON-ответ модели (терпим к ```json ограждениям).

    Блоки <reasoning>/<think> вырезаются ДО поиска JSON: жадный `\\{.*\\}`
    начинается с первой фигурной скобки в тексте, и скобка внутри рассуждения
    уводила парсер мимо настоящего ответа.
    """
    if not raw:
        return None
    raw = _REASONING_SPAN_RE.sub("\n", raw)
    raw = _re.sub(r"<think>.*?</think>", "\n", raw, flags=_re.DOTALL)
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
    raw_ids = obj.get("source_ids")
    source_ids = [s for s in raw_ids if isinstance(s, str)] if isinstance(raw_ids, list) else []
    return {
        "action": obj["action"],
        "suit": obj.get("suit", "").strip(),
        "client": obj.get("client", "").strip(),
        "memo": obj.get("memo", "").strip(),
        "confidence": max(0, min(100, conf)),
        "confidence_reason": str(obj.get("confidence_reason", "")).strip(),
        "analysis": str(obj.get("analysis", "")).strip(),
        "source_ids": source_ids,
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

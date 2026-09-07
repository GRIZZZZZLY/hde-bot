"""Карта ответственности: симптом → кто отвечает → что сказать клиенту.

Разбор жалобы владельца 2026-09-07. Бот написал «передадим вашему профильному
специалисту», оператор — «обращайтесь в банк, Posiflora QR на терминале не
генерирует». Владелец и есть тот специалист, и вердикт «неверный факт» отправил
эту пару в очередь кандидатов в базу знаний, где ей делать нечего.

Правильное место для такого знания — не база знаний, а промпт. Причины две:

1. Это правило маршрутизации, а не решение проблемы. Оно короткое, стабильное
   («QR на терминале — всегда банк») и почти не зависит от формулировки тикета.
2. Retrieval молчит именно тогда, когда тема новая, — то есть в тот самый
   момент, когда бот и уходит в эскалацию. Правило, доступное только через
   поиск, в этот момент не сработает.

Карта собирается офлайн (scripts/build_routing_map.py), вычитывается человеком и
кладётся в data/routing_map.json. Отсутствие файла — рабочий режим: блок пустой,
промпт как был.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "routing_map.json"

# Адресаты, которые встречаются в ответах операторов. Список открытый — это
# нормализация регистра, а не валидация: незнакомый адресат лучше выкинутого.
_KNOWN_OWNERS = {
    "мы": "мы",
    "банк": "банк",
    "офд": "ОФД",
    "вендор": "вендор",
    "вендор кассы": "вендор кассы",
    "оператор связи": "оператор связи",
    "1с": "1С",
    "клиент": "клиент сам",
    "клиент сам": "клиент сам",
}

# Лексика ответов, которыми оператор переводит вопрос на чужую сторону. Ищем
# именно адресацию, а не любое упоминание банка: «сбились настройки, я поправил»
# тоже содержит слово «настройки», но маршрутизацией не является.
_ROUTING_RE = re.compile(
    r"обраща\w+\s+(?:в|к)\s+\w+|обратитесь\s+(?:в|к)\s+\w+|"
    r"это\s+вопрос\s+к\s+\w+|"
    r"(?:никак\s+)?не\s+связан\w*\s+с|не\s+наш\w*\s+(?:зона|ответственност|ПО|программ)|"
    r"(?:это|тут)\s+(?:их|банка|офд)\s+\w+|"
    r"обратитесь\s+в\s+техподдержку",
    re.I,
)

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RoutingRule:
    symptom: str
    owner: str
    say: str


def looks_like_routing(text: str) -> bool:
    """Похож ли ответ оператора на маршрутизацию «это не к нам»."""
    return bool(_ROUTING_RE.search(text or ""))


def _norm_owner(owner: str) -> str:
    value = (owner or "").strip()
    return _KNOWN_OWNERS.get(value.lower(), value)


def _norm_key(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def parse_routing_map(raw) -> list[RoutingRule]:
    """Правила из сырых словарей. Неполные молча отбрасываются.

    Неполное правило в промпте вреднее отсутствующего: «симптом → (пусто)»
    модель достроит сама, и достроит неправильно.
    """
    rules: list[RoutingRule] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        symptom = (item.get("symptom") or "").strip()
        owner = _norm_owner(item.get("owner") or "")
        say = (item.get("say") or "").strip()
        if not (symptom and owner and say):
            continue
        rules.append(RoutingRule(symptom=symptom, owner=owner, say=say))
    return rules


def dedup_rules(rules: list[RoutingRule]) -> list[RoutingRule]:
    """Один симптом + один адресат = одно правило.

    Один симптом с РАЗНЫМИ адресатами не склеивается: это противоречие, и
    вычитывающий человек должен его увидеть, а не получить произвольную из двух
    строк.
    """
    seen: set[tuple[str, str]] = set()
    out: list[RoutingRule] = []
    for rule in rules:
        key = (_norm_key(rule.symptom), _norm_key(rule.owner))
        if key in seen:
            continue
        seen.add(key)
        out.append(rule)
    return out


def load_routing_map(path=None) -> list[RoutingRule]:
    """Карта из JSON. Пустой список при любой беде — черновик важнее карты.

    Формат: массив объектов либо {"rules": [...]}. Второй вариант — чтобы в
    файл можно было положить дату сборки и не ломать чтение.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("routing map: не прочитан %s: %s", target, exc)
        return []
    if isinstance(data, dict):
        data = data.get("rules") or []
    return dedup_rules(parse_routing_map(data))


def format_routing_block(rules) -> str:
    """Блок для системного промпта. Пустая строка, если карты нет."""
    if not rules:
        return ""
    lines = [
        "Кто за что отвечает. Если симптом клиента совпал со строкой ниже — "
        "отвечай по ней и НЕ эскалируй на специалиста:",
    ]
    for rule in rules:
        lines.append(f"— {rule.symptom}: отвечает {rule.owner} → {rule.say}")
    return "\n".join(lines)

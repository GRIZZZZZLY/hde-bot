"""Помощники контекста агента v2 (spec 2026-09-27 §3, п.2).

Всё чистое и тестируемое. Причины, по которым это вообще понадобилось, — в
docs/agent-review-2026-09-27.md: поиск шёл по телефону или «Хорошо» (51%
черновиков), модель видела начало статьи вместо шагов, а макросы первой линии
в истории учили её отправлять клиента ждать специалиста.
"""
from __future__ import annotations

import re

from .operator_text import BOILERPLATE_RE, CLOSER_RE, clean_operator_text, strip_html

_WORD_RE = re.compile(r"[а-яёa-z]+", re.I)
_ACK_WORDS = frozenset({
    "хорошо", "спасибо", "ок", "окей", "ok", "да", "нет", "ждем", "ждём", "жду",
    "благодарю", "понял", "поняла", "понятно", "ясно", "сейчас", "минуту", "секунду",
    "отлично", "принято", "верно", "все", "всё", "так", "ани", "деск", "анидеск",
    "рудесктоп", "номер", "телефон", "вот", "пожалуйста",
})
_MIN_LETTERS = 12

_REMOTE_INSTRUCTION_RE = re.compile(
    r"(скачайте|установите|запустите|откройте)[^.\n]{0,60}"
    r"(anydesk|rudesktop|анидеск|рудесктоп|удал[её]нн\w+ доступ)",
    re.I,
)
_REMOTE_MARKER = "[ранее предложено удалённое подключение]"
_STAFF_PREFIX = "Сотрудник:"

# \b в начале: «снова» внутри «основании»; стоим\b: «стоимость»; «в первую очередь» — не стресс
_STRESS_RE = re.compile(
    r"\b(?:опять|снова|вчера уже|до сих пор|который раз|(?<!первую\s)очеред|закрыть смену|"
    r"смену закрыть|не (?:можем|могу) (?:продавать|пробить|работать)|клиенты ждут|стоим\b|"
    r"срочно|где ваш|сколько можно|жду уже|никто не (?:отвечает|ответил))|!!",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|приветствую)[!.,]*\s*", re.I
)
_STOP_WORDS = frozenset({"после", "перед", "когда", "почему", "подскажите", "пожалуйста"})


def is_substantive(text: str) -> bool:
    """Содержательное сообщение клиента: не телефон, не ID, не подтверждение."""
    words = _WORD_RE.findall((text or "").lower())
    if sum(len(w) for w in words) < _MIN_LETTERS:
        return False
    return not all(w in _ACK_WORDS for w in words)


def client_messages(posts, client_id) -> list[str]:
    ordered = sorted(posts, key=lambda p: int(getattr(p, "post_id", 0) or 0))
    return [
        strip_html(getattr(p, "text", ""))
        for p in ordered
        if not getattr(p, "is_comment", False)
        and str(getattr(p, "user_id", "")) == str(client_id)
    ]


def staff_messages(posts, client_id) -> list[str]:
    ordered = sorted(posts, key=lambda p: int(getattr(p, "post_id", 0) or 0))
    return [
        strip_html(getattr(p, "text", ""))
        for p in ordered
        if not getattr(p, "is_comment", False)
        and str(getattr(p, "user_id", "")) != str(client_id)
    ]


# Состояние тикета считает код, а не модель: в проверках v2 (разбор §11–12) модель
# повторно просила уже присланный AnyDesk, выдумывала «перезагрузка не помогла» и
# почти не давала быстрый шаг оператора, хотя список таких шагов лежал в промпте.
_DIGIT_RUN_RE = re.compile(r"\+?\d[\d  ()\-]*\d")
_QUOTE_RE = re.compile(r"support posiflora|posiflorasupportbot|скачайте программу", re.I)
_REMOTE_APP_RE = re.compile(
    r"anydesk|ани\s*деск|энидеск|rudesktop|rudekstop|рудеск\w*|программ\w*.{0,20}установлен", re.I
)
_REBOOT_RE = re.compile(r"перезагру|перезапус", re.I)
_OTHER_PORT_RE = re.compile(r"(?:друг\w*|ин\w+)\s+(?:usb|порт|разъ[её]м)|переподключ", re.I)
_NETWORK_RE = re.compile(r"\bсеть\b|по сети|через интернет|wi-?fi|wu-?fi|вай[\s-]?фай", re.I)

_PRINTER_RE = re.compile(
    r"не\s*печата|порт\w* недоступ|inout|ин[ао]ут|печатн\w* док|закры\w* смен|"
    r"смен\w* не закрыва|пробить чек", re.I)
_TERMINAL_RE = re.compile(
    r"терминал\w*\s+не\s+отвеча|невозможно подключиться к устройству|на терминал не|"
    r"не\s+выв\w+ на терминал|оплат\w* не (?:проход|выход)|отказано", re.I)
_SERVICE_RE = re.compile(r"(?:слетел|пропал|нет|удал\w*)\W+(?:\w+\W+){0,2}(?:posiflora|посифлор\w*)\s*"
                         r"(?:service|сервис)|(?:posiflora|посифлор\w*)\s*(?:service|сервис)\W+"
                         r"(?:\w+\W+){0,2}(?:слетел|пропал|нет|удал)", re.I)
_OFD_RE = re.compile(r"\bофд\b|налогов|ресурс\w* хранени|фд исчерпан", re.I)
_LABEL_RE = re.compile(r"этикет\w*.{0,30}(?:смещ|съезж|сдвиг|криво)", re.I)
_HANG_RE = re.compile(r"(?:posiflora|посифлор\w*).{0,20}завис|завис\w*.{0,20}(?:posiflora|посифлор)", re.I)

def _reboot_quote(messages: list[str]) -> str:
    """Слова клиента вокруг «перезагру…»: по 4 слова с каждой стороны, без обрывков."""
    for m in messages:
        words = (m or "").split()
        for i, w in enumerate(words):
            if _REBOOT_RE.search(w):
                return " ".join(words[max(0, i - 4): i + 5])
    return ""


def _remote_id_sent(messages: list[str]) -> bool:
    """9 цифр или 10 с единицы — номер AnyDesk/RuDesktop; 11 цифр или 10 с девятки — телефон."""
    for m in messages:
        for run in _DIGIT_RUN_RE.findall(m or ""):
            digits = re.sub(r"\D", "", run)
            if "(" in run or run.startswith("+"):
                continue
            if len(digits) == 9 or (len(digits) == 10 and digits[0] == "1"):
                return True
    return False


def _first_step(text: str, client_text: str, all_text: str, rebooted: bool) -> str | None:
    """Первый шаг по симптому: None — симптом не узнан, "" — типовой шаг уже пробовали.

    ponytail: регулярки по 23 тикетам первой проверки; новый частый симптом — строка сюда.
    """
    if _PRINTER_RE.search(text):
        if "эвотор" in text.lower():
            return "" if rebooted else "перезагрузить кассу и снова напечатать чек"
        if _NETWORK_RE.search(client_text):
            return "" if rebooted else "перезагрузить Wi-Fi роутер и кассу, затем повторить печать"
        return ("" if _OTHER_PORT_RE.search(all_text)
                else "кабель кассы в другой USB-разъём компьютера, затем перезагрузить кассу")
    if _TERMINAL_RE.search(text):
        return "" if rebooted else "перезагрузить Wi-Fi роутер и терминал, повторить оплату"
    if _SERVICE_RE.search(text):
        return "проверить подписку на Posiflora Service в личном кабинете Эвотора"
    if _OFD_RE.search(text):
        return "проверить, оплачен ли тариф в личном кабинете ОФД"
    if _HANG_RE.search(text):
        return "" if rebooted else "закрыть и снова открыть Posiflora"
    if _LABEL_RE.search(text):
        return "прислать фото, как установлена лента"
    return None


def ticket_state(title: str, client_msgs: list[str], staff_msgs: list[str]) -> str:
    """Что уже прислано и сделано + рекомендованный первый шаг. Пусто — нечего сказать."""
    own = [m for m in client_msgs if not _QUOTE_RE.search(m or "")]
    client_text = "\n".join(client_msgs)
    text = f"{title}\n{client_text}"
    lines: list[str] = []
    remote_id = _remote_id_sent(own)
    if remote_id:
        lines.append("Клиент уже прислал номер для удалённого подключения — не проси его снова.")
    elif any(_REMOTE_APP_RE.search(m) for m in own):
        lines.append("Клиент пишет, что программа для удалённого доступа у него есть.")
    reboot = _reboot_quote(client_msgs)
    if reboot:
        lines.append(f"Клиент уже перезагружал: «{reboot}».")
    step = _first_step(text, client_text, text + "\n" + "\n".join(staff_msgs), bool(reboot))
    if step:
        lines.append(f"Рекомендованный первый шаг: {step}.")
    elif step == "":
        nxt = "подключиться по присланному номеру" if remote_id else "удалённое подключение"
        lines.append(f"Типовой первый шаг уже пробовали, следующий — {nxt}.")
    return "\n".join(lines)


def build_retrieval_query(title: str, messages: list[str]) -> str:
    useful = [m for m in messages if is_substantive(m)]
    parts = [title or ""]
    if useful:
        parts.append(useful[0])
        if useful[-1] != useful[0]:
            parts.append(useful[-1])
    return "\n".join(p for p in parts if p)[:600]


def strip_history_macros(history: str) -> str:
    """Штампы первой линии и бота — вон, инструкция по удалёнке — одной меткой."""
    kept: list[str] = []
    for line in (history or "").splitlines():
        if not line.startswith(_STAFF_PREFIX):
            kept.append(line)
            continue
        body = line[len(_STAFF_PREFIX):].strip()
        if BOILERPLATE_RE.search(body):
            continue
        if _REMOTE_INSTRUCTION_RE.search(body):
            kept.append(f"{_STAFF_PREFIX} {_REMOTE_MARKER}")
            continue
        cleaned = clean_operator_text(body)
        if cleaned:
            kept.append(f"{_STAFF_PREFIX} {cleaned}")
    return "\n".join(kept)


def _query_terms(query: str) -> set[str]:
    return {
        w[:5] for w in _WORD_RE.findall((query or "").lower())
        if len(w) >= 4 and w not in _STOP_WORDS
    }


def best_chunk(content: str, query: str, target: int = 700) -> str:
    """Кусок статьи с наибольшим числом слов запроса.

    ponytail: лексический выбор по основам слов (5 букв), без эмбеддингов на
    каждый кусок; если промахивается — эмбеддить куски при индексации.
    """
    from ..knowledge.chunker import chunk_markdown

    content = (content or "").strip()
    if len(content) <= target:
        return content
    chunks = chunk_markdown(content, target=target, overlap=100) or [content[:target]]
    terms = _query_terms(query)

    def score(chunk: str) -> int:
        stems = {w[:5] for w in _WORD_RE.findall(chunk.lower())}
        return len(terms & stems)

    best = max(chunks, key=score)          # max берёт первый при равенстве
    return best[: int(target * 1.5)]


def detect_stress(messages: list[str]) -> bool:
    return any(_STRESS_RE.search(m or "") for m in messages)


def is_first_staff_reply(posts, client_id) -> bool:
    """Сотрудник ещё ничего содержательного клиенту не писал (штампы не в счёт)."""
    for p in posts:
        if getattr(p, "is_comment", False):
            continue
        if str(getattr(p, "user_id", "")) == str(client_id):
            continue
        if clean_operator_text(strip_html(getattr(p, "text", ""))):
            return False
    return True


def clean_demo_answer(text: str) -> str:
    text = _GREETING_RE.sub("", strip_html(text or ""))
    text = CLOSER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()

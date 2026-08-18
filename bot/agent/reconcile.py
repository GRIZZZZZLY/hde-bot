"""Ночная сверка: предложенный ботом ответ ↔ фактический ответ оператора в тикете.

Операторы не жмут кнопки бота и не отвечают через него (см. память
optimization-samples-stale) — поэтому сигнал для обучения берём иначе: после смены
сверяем, что бот предложил, с тем, что оператор реально написал клиенту в тикете.
Ноль участия операторов, человеческий фактор исключён.

Две вещи, без которых сверка мерит шум (проверено на 128 сверках за 30 дней):

1. Эталоном сплошь оказываются реплики, которые ответом не являются: макрос
   «Примите запрос на компьютере» (22 раза из 128), «не могу дозвониться»,
   «вопрос актуален?», «Хорошо»/«🤝», рассылки. Их отсеиваем ДО сравнения —
   пара уходит в skipped, а не в «расхождение».
2. Косинус e5 на этих текстах лежит в 0.75–0.94 (пол модели ~0.8), то есть
   порог 0.85 режет шум: пары с одинаковым смыслом получали противоположные
   вердикты. Вместо порога — LLM-судья с категориями, которые сразу говорят,
   ЧТО не так: bot_escalated (оператор решил сам) / bot_wrong_fact (неверно по
   существу) / same_action / not_comparable.

Ядро (этот модуль) чистое и тестируемое; обвязка (БД/HDE API/расписание) — в
reconcile_recent + scheduler.
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import re as _re

from .dialogue_mining import is_staff_post, sort_posts

logger = logging.getLogger(__name__)

# Короче этого (после чистки) содержательного ответа не бывает: остаются
# «Связался», «Получилось?», «Хорошо» и прочие подтверждения.
MIN_MEANINGFUL_CHARS = 12

# Штампы бота/первой линии/диспетчера в тикете — не «ответ оператора».
_BOILERPLATE_RE = _re.compile(
    r"принят[оа] в работу|передан[оа].*специалист|свяжется с вами|"
    r"специалист свяжется|ожидайте.*(ответ|чат)|благодарим за (информаци|ожидани|предостав)",
    _re.I,
)

# Закрывашки в конце ответа — вырезаются фразой (не строкой): часто приклеены
# к содержательному тексту. Ответ из одних закрывашек → None (тикет решён вне
# переписки, судить не по чему).
_CLOSER_RE = _re.compile(
    r"(?:подскажите,?\s*)?могу (?:вам )?[её]щ[её] чем-то помочь\s*\??|"
    r"всегда рады помочь[!.]?|"
    r"обращайтесь,? если у вас возникнут вопросы[!.]?|"
    r"будут [её]щ[её] вопросы\s*[—–-]?\s*обращайтесь[!.]?",
    _re.I,
)

# Макросы операторов: обслуживание сеанса связи, а не ответ на вопрос клиента.
# Вырезаются фразой — в одном посте макрос часто склеен с содержательным текстом
# («Примите запрос на компьютере Обновил вам драйвер. Пробуйте печатать чек»).
_MACRO_RE = _re.compile(
    # запуск удалённого сеанса
    r"прим(?:и|е)те запрос(?:\s+(?:на компьютере|ещ[её] раз))*(?:,?\s*я к вам подключаюсь)?|"
    r"необходимо удал[её]нно подключиться к вашему компьютеру[\s\S]*?номер рабочего места\.?|"
    r"скачайте программу для удал[её]нного доступа[\s\S]*?номер рабочего места\.?|"
    # сеанс не состоялся
    r"не (?:могу|получается) (?:к вам )?подключиться|"
    r"(?:программа\s+\S+\s+(?:запущена|включена)[,\s]*(?:и\s*)?)?"
    r"(?:на компьютере есть интер\w+|интер\w+ есть на компьютере)|"
    r"(?:по этому номеру\s*)?не (?:могу|смог|смогли)[^.?!]{0,40}дозвонит?ься"
    r"(?:[^.?!]{0,30}\d[\d\s()+-]*)?|"
    r"(?:с вами\s*)?по какому номеру (?:могу )?(?:с вами )?связаться|"
    r"(?:(?:есть|или)\s+)?(?:другой\s+)?номер для связи|"
    r"(?:подскажите,?\s*)?номер (?:для связи )?актуальн\w*|"
    r"напишите,? когда (?:могу|мне|нужно будет|можно)\s*перезвонить|"
    # добор молчащего клиента
    r"не получил(?:и)?\s*ответ\w*\s*на последнее сообщение|"
    r"если возникнут вопросы,? напишите мне снова(?:,?\s*я буду здесь)?|"
    r"(?:подскажите|скажите)?,?\s*(?:пожалуйста,?\s*)?вопрос (?:ещ[её] )?актуален|"
    r"жд[её]м ответа,? чтобы помочь вам|"
    r"жду от вас обратную связь|"
    # обещание перезвонить/подключиться
    r"(?:через\s*)?\d+\s*[-–—]?\s*\d*\s*минут\w*[,\s]*(?:и\s*)?"
    r"(?:подключ\w+|перезвоню|свяжусь|вернусь)(?:\s*(?:к|с)\s*вам\w*)?|"
    r"(?:подключ(?:усь|юсь)|перезвоню|свяжусь|вернусь)\s*(?:к\s*вам|с\s*вами|вам)?\s*"
    r"(?:через|в течени[еи])\s*\d+\s*[-–—]?\s*\d*\s*минут\w*|"
    r"ещ[её] немного времени и вернусь к вам|"
    r"сейчас завершу текущее подключение и подключусь к вам|"
    # статус без содержания
    r"связал(?:ся|ись)(?:\s*с\s*\S+)?|подключился к вам|(?:\S+\s+)?созвонились|"
    # рассылки
    r"витрина стала современнее[\s\S]*|сделайте свою витрину[\s\S]*|хороших продаж[!.]?",
    _re.I,
)

_ACK_RE = _re.compile(
    r"^(?:хорошо|спасибо|отлично|принято|понял|понятно|ясно|да|ок)"
    r"(?:[,\s]+(?:спасибо|хорошо|понял))?[.!]*$",
    _re.I,
)

_HAS_LETTER_RE = _re.compile(r"[a-zA-Zа-яА-ЯёЁ]")

_CATEGORIES = ("same_action", "bot_escalated", "bot_wrong_fact", "not_comparable")

# same_action = бот ≈ человек (accepted); расхождение → эталон = человек (corrected).
_CATEGORY_LABEL = {
    "same_action": "accepted",
    "bot_escalated": "corrected",
    "bot_wrong_fact": "corrected",
}


def _strip_html(text: str) -> str:
    cleaned = _re.sub(r"<[^>]+>", " ", text or "")
    cleaned = _html.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def _is_meaningless(text: str) -> bool:
    """Подтверждение, эмодзи, обрывок — сравнивать не с чем."""
    stripped = text.strip(" .,!?—–-")
    if not _HAS_LETTER_RE.search(stripped):
        return True
    if _ACK_RE.match(stripped):
        return True
    return len(stripped) < MIN_MEANINGFUL_CHARS


def _clean_answer(text: str) -> str:
    """Оставляет содержательный текст: без штампов, закрывашек и макросов.

    Пост оператора = строка, поэтому чистим построчно: макрос-в-отдельном-посте
    отбрасывается целиком, а макрос, склеенный с ответом, — вырезается фразой.
    """
    kept = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or _BOILERPLATE_RE.search(line):
            continue
        residue = _MACRO_RE.sub(" ", _CLOSER_RE.sub(" ", line))
        residue = _re.sub(r"\s+", " ", residue)
        # Вырезанная фраза оставляет висячую пунктуацию («перезагрузили? . .»):
        # знак, оторванный от слова пробелом или стоящий в начале, — мусор,
        # приклеенный к слову — часть уцелевшего текста.
        residue = _re.sub(r"(?:^|\s)[.,!?;:—–-]+", " ", residue)
        residue = _re.sub(r"([.,!?;:])[.,!?;:]+", r"\1", residue)
        residue = _re.sub(r"\s+", " ", residue).strip()
        if residue and not _is_meaningless(residue):
            kept.append(residue)
    return " ".join(kept).strip()


def _post_id(post) -> int:
    try:
        return int(getattr(post, "post_id", 0))
    except (TypeError, ValueError):
        return 0


def find_operator_reply_after(posts, anchor_post_id, staff: set[str]) -> str | None:
    """Первый содержательный ответ оператора ПОСЛЕ якоря (context_until_post_id).

    Якорь — последний пост, что бот видел при генерации. Реальный ответ оператора —
    первый staff-turn с post_id > якоря (подряд идущие staff-посты склеиваются).
    Штампы, макросы и подтверждения отфильтровываются; если содержательного ответа
    нет — None (тикет ещё в работе / оператор обслуживал сеанс связи → не судим).
    """
    try:
        anchor = int(anchor_post_id)
    except (TypeError, ValueError):
        anchor = 0
    ordered = [
        p for p in sort_posts(posts)
        if not getattr(p, "is_comment", False) and _post_id(p) > anchor
    ]
    i, n = 0, len(ordered)
    while i < n and not is_staff_post(ordered[i], staff):
        i += 1
    turn = []
    while i < n and is_staff_post(ordered[i], staff):
        turn.append(ordered[i])
        i += 1
    if not turn:
        return None
    text = _clean_answer("\n".join(_strip_html(p.text) for p in turn))
    return text or None


def _build_judge_prompt(ai_answer: str, reference: str) -> tuple[str, str]:
    system = (
        "Ты сверяешь черновик ассистента с тем, что реально написал оператор "
        "техподдержки кассового ПО тому же клиенту в том же тикете.\n"
        "same_action — по сути одно и то же действие или уточнение; формулировки "
        "и порядок слов могут отличаться.\n"
        "bot_escalated — оператор решил вопрос сам (подключился, дал инструкцию, "
        "назвал причину), а ассистент отправил ждать специалиста или звонка.\n"
        "bot_wrong_fact — ассистент неверен по существу: не та модель или ПО, не тот "
        "адресат (банк вместо нас или наоборот), неверный порядок действий, "
        "несуществующая настройка.\n"
        "not_comparable — сравнивать нечего: реплики про разное, оператор продолжил "
        "свою ветку или ответил на другой вопрос.\n"
        'Верни СТРОГО JSON: {"category":"same_action|bot_escalated|bot_wrong_fact|'
        'not_comparable","reason":"кратко по-русски"}'
    )
    user = (
        f"Черновик ассистента:\n{(ai_answer or '')[:1200]}\n\n"
        f"Ответ оператора:\n{(reference or '')[:1200]}"
    )
    return system, user


async def judge_divergence(
    ai_answer: str, reference: str, *, _call_fn=None
) -> tuple[str, str] | None:
    """Категория расхождения от LLM. None при сбое/мусорном JSON — пара не судится."""
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_judge_prompt(ai_answer, reference)
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = _json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("category") not in _CATEGORIES:
        return None
    return obj["category"], str(obj.get("reason", ""))[:200]


async def reconcile_one(
    *, ai_answer: str, posts, anchor_post_id, staff: set[str], _judge_fn=None
) -> dict | None:
    """Сверка одного предложения. None, если сравнивать не с чем (нет ответа
    оператора / реплика оператора ответом не является) или судья не ответил."""
    if _judge_fn is None:
        _judge_fn = judge_divergence
    reference = find_operator_reply_after(posts, anchor_post_id, staff)
    if not reference:
        return None
    verdict = await _judge_fn(ai_answer, reference)
    if verdict is None:
        return None
    category, reason = verdict
    return {"reference_answer": reference, "category": category, "reason": reason}


async def reconcile_recent(
    *, hours: int = 24, _suggestions_fn=None, _posts_fn=None, _set_fn=None,
    _staff=None, _judge_fn=None, _sleep_fn=None,
    pause_s: float = 4.0, retry_pause_s: float = 30.0,
) -> dict:
    """Ночная сверка свежих предложений с фактическими ответами операторов.

    Тянет посты через HDE API (под троттлом), судит LLM-судьёй, пишет
    judge_reference_answer + канонический judge_label (accepted/corrected) и
    категорию в judge_detail. not_comparable не размечается — только считается.
    Пейсинг под Groq free-tier: pause_s между парами, один повтор после
    retry_pause_s. Ошибка по одному тикету не роняет проход.
    """
    from ..config import config

    if _suggestions_fn is None:
        from ..db import get_unjudged_suggestions as _suggestions_fn
    if _set_fn is None:
        from ..db import set_judge_result as _set_fn
    if _judge_fn is None:
        _judge_fn = judge_divergence
    if _sleep_fn is None:
        import asyncio
        _sleep_fn = asyncio.sleep
    if _posts_fn is None:
        from ..hde_api import HDEApiClient
        _client = HDEApiClient()

        async def _posts_fn(ticket_id):
            return await _client.get_all_ticket_posts(ticket_id)

    staff = set(_staff) if _staff is not None else {
        str(config.hde_owner_id), *config.agent_staff_user_ids
    }

    stats = {"same_action": 0, "bot_escalated": 0, "bot_wrong_fact": 0,
             "not_comparable": 0, "skipped": 0, "errors": 0}
    for i, sug in enumerate(await _suggestions_fn(hours=hours)):
        try:
            posts = await _posts_fn(str(sug["ticket_id"]))
            reference = find_operator_reply_after(
                posts, sug.get("context_until_post_id"), staff
            )
            if not reference:
                stats["skipped"] += 1
                continue
            if i and pause_s:
                await _sleep_fn(pause_s)
            ai_answer = sug.get("ai_answer") or ""
            verdict = await _judge_fn(ai_answer, reference)
            if verdict is None:
                await _sleep_fn(retry_pause_s)
                verdict = await _judge_fn(ai_answer, reference)
            if verdict is None:
                stats["skipped"] += 1
                continue
            category, reason = verdict
            stats[category] += 1
            label = _CATEGORY_LABEL.get(category)
            if label is None:
                continue  # not_comparable: судить не по чему, метку не ставим
            await _set_fn(
                sug["id"],
                reference_answer=reference,
                label=label,
                detail=f"judge:{category} {reason}".strip(),
            )
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("reconcile: ticket %s failed: %s", sug.get("ticket_id"), exc)
    return stats

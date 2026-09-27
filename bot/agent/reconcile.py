"""Ночная сверка: предложенный ботом ответ ↔ фактический ответ оператора в тикете.

Операторы не жмут кнопки бота и не отвечают через него (см. память
optimization-samples-stale) — поэтому сигнал для обучения берём иначе: после смены
сверяем, что бот предложил, с тем, что оператор реально написал клиенту в тикете.
Ноль участия операторов, человеческий фактор исключён.

Две вещи, без которых сверка мерит шум (проверено на 128 сверках за 30 дней):

1. Эталоном сплошь оказываются реплики, которые ответом не являются: макрос
   «Примите запрос на компьютере» (22 раза из 128), «не могу дозвониться»,
   «вопрос актуален?», «Хорошо»/«🤝», рассылки. Их отсеиваем ДО сравнения —
   пара уходит в skipped, а не в «расхождение». Правила отсева — в
   operator_text, общие с нарезкой пар (dialogue_mining).
2. Косинус e5 на этих текстах лежит в 0.75–0.94 (пол модели ~0.8), то есть
   порог 0.85 режет шум: пары с одинаковым смыслом получали противоположные
   вердикты. Вместо порога — LLM-судья с категориями, которые сразу говорят,
   ЧТО не так: bot_escalated (оператор решил сам) / bot_wrong_fact (неверно по
   существу) / same_action / not_comparable.
3. Судья обязан видеть то, что видел оператор. Пока он получал только вопрос
   клиента и два ответа, любое решение, опёртое на скриншот, комментарий коллеги
   или телефонный разговор, размечалось как ошибка бота и уезжало в очередь
   кандидатов в базу знаний. Отсюда категория context_gap: она отвечает не
   «черновик плох», а «этого канала контекста у бота не было», метку качества не
   получает и в базу знаний не идёт.

Ядро (этот модуль) чистое и тестируемое; обвязка (БД/HDE API/расписание) — в
reconcile_recent + scheduler.
"""
from __future__ import annotations

import json as _json
import logging

from .dialogue_mining import is_staff_post, sort_posts
from .operator_text import clean_operator_text, strip_html

logger = logging.getLogger(__name__)

_CATEGORIES = (
    "same_action", "bot_better", "bot_escalated", "bot_wrong_fact", "context_gap",
    "not_comparable",
)

# same_action = бот ≈ человек (accepted); расхождение → эталон = человек (corrected).
# context_gap и not_comparable метки не получают: в первом случае бот проиграл не
# по качеству, а по осведомлённости, во втором сравнивать нечего. Метка тут
# означала бы «черновик плох», и эта ложь уехала бы в датасет офлайн-оценки.
_CATEGORY_LABEL = {
    "same_action": "accepted",
    "bot_better": "accepted",
    "bot_escalated": "corrected",
    "bot_wrong_fact": "corrected",
}

_REG_KEYS = ("ack", "one_step", "why", "risk", "check_back", "no_conveyor", "no_invented_promise")

# Каналы контекста, которых боту не хватило. Значение вне списка — фантазия
# модели, сводим к other: терять из-за него весь вердикт незачем.
_MISSING_CHANNELS = ("screenshot", "comment", "call", "client_msg", "other")


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
    text = clean_operator_text("\n".join(strip_html(p.text) for p in turn))
    return text or None


def collect_after_anchor(
    posts, anchor_post_id, staff: set[str], client_id: str = ""
) -> dict:
    """Что появилось в тикете после якоря и ДО ответа оператора.

    Судья без этого сравнивал два ответа так, будто у бота и у оператора был один
    контекст. Его не было: оператор к моменту ответа читал комментарий коллеги,
    смотрел вложение и мог созвониться. Разметив это как ошибку бота, сверка
    заводила кандидата в базу знаний на факте, которого в тикете нет.

    Граница — тот же staff-turn, который берётся эталоном (см.
    find_operator_reply_after): всё, что легло ПОСЛЕ ответа оператора, на его
    решение повлиять не могло.
    """
    try:
        anchor = int(anchor_post_id)
    except (TypeError, ValueError):
        anchor = 0
    client_messages: list[str] = []
    colleague_comments: list[str] = []
    attachment_names: list[str] = []
    for post in sort_posts(posts):
        if _post_id(post) <= anchor:
            continue
        is_comment = bool(getattr(post, "is_comment", False))
        if not is_comment and is_staff_post(post, staff):
            break  # начался ответ оператора — дальше уже не его входные данные
        text = strip_html(getattr(post, "text", ""))
        for f in getattr(post, "files", None) or []:
            name = (f or {}).get("name") if isinstance(f, dict) else None
            if name:
                attachment_names.append(str(name))
        if not text:
            continue
        if is_comment:
            colleague_comments.append(text)
        else:
            client_messages.append(text)
    return {
        "client_messages": client_messages,
        "colleague_comments": colleague_comments,
        "attachment_names": attachment_names,
        "has_attachments": bool(attachment_names),
    }


def _build_judge_prompt(
    ai_answer: str,
    reference: str,
    client_text: str = "",
    *,
    after_context: dict | None = None,
    photo_descriptions: str = "",
    call_notes: str = "",
) -> tuple[str, str]:
    system = (
        "Ты сверяешь черновик ассистента с тем, что реально написал оператор "
        "техподдержки кассового ПО тому же клиенту в том же тикете.\n"
        "Черновик ассистент писал РАНЬШЕ, чем оператор свой ответ, и видел меньше.\n"
        "same_action — по сути одно и то же действие или уточнение; формулировки "
        "и порядок слов могут отличаться.\n"
        "bot_better — по сути черновик точнее или безопаснее ответа оператора (оператор "
        "тоже ошибается; ответы написаны до нового регламента общения).\n"
        # context_gap стоит ДО bot_wrong_fact намеренно: найдя «неверный факт»,
        # модель вердикт уже не переоценивает, и «бот не мог знать» превращается
        # в «бот ошибся» — ровно тот шум, из-за которого владелец каждое утро
        # решал по фактам, которых в тикете не было.
        "context_gap — ответ оператора опирается на то, чего у ассистента в "
        "контексте НЕ БЫЛО: содержимое скриншота или файла, комментарий коллеги, "
        "разговор по телефону, сообщение клиента, пришедшее после черновика. "
        "Ставь эту категорию, когда можешь назвать, чего именно не хватало; "
        "ошибкой факта это не считается.\n"
        "bot_escalated — оператор решил вопрос сам (подключился, дал инструкцию, "
        "назвал причину), а ассистент отправил ждать специалиста или звонка. "
        "Если решение оператора стало возможным только из-за контекста, которого "
        "у ассистента не было, это context_gap, а не bot_escalated.\n"
        "bot_wrong_fact — ассистент неверен ПО ФАКТУ: не та модель или ПО, не тот "
        "адресат (банк вместо нас или наоборот), несуществующая настройка, неверное "
        "значение параметра, инструкция, которая физически не сработает. Ставь "
        "только если факт был проверяем по тому, что ассистенту дали.\n"
        "not_comparable — сравнивать нечего: реплики про разное, оператор продолжил "
        "свою ветку или ответил на другой вопрос.\n"
        # Сводка 2026-09-03: судья звал bot_wrong_fact там, где бот добавил
        # лишний шаг («печать X-отчёта») или спросил не то, что оператор. Это
        # разница в объёме, а не ошибка факта, и она уводила пару в очередь
        # кандидатов в базу знаний, где такой строке делать нечего.
        "ВАЖНО: лишний или недостающий шаг сам по себе НЕ bot_wrong_fact. Если "
        "основное действие совпадает — это same_action. Разный порядок слов и объём "
        "на категорию не влияют: форму оценивает отдельный чек-лист ниже. Ставь "
        "bot_wrong_fact только когда "
        "можешь назвать конкретный неверный факт.\n"
        "Если ответ оператора — «да»/«нет»/короткое подтверждение без содержания, "
        "это not_comparable: сверять не с чем.\n"
        "Если черновик выдумал обещание (инженер свяжется, звонок, срок) или отправил "
        "клиента ждать специалиста, а оператор дал шаг — это bot_escalated, даже если "
        "контекста не хватало.\n"
        "Отдельно оцени ФОРМУ черновика по регламенту (не сравнивая с оператором), "
        "каждый пункт yes|no|na: ack — признал конкретное неудобство, если клиент "
        "раздражён или спешит; one_step — один шаг или один вопрос; why — объяснил зачем, "
        "если просит данные; risk — предупредил о риске до шага; check_back — попросил "
        "проверить результат после инструкции; no_conveyor — нет дежурных фраз; "
        "no_invented_promise — нет выдуманных обещаний.\n"
        'Верни СТРОГО JSON: {"category":"same_action|bot_better|context_gap|bot_escalated|'
        'bot_wrong_fact|not_comparable","missing":"screenshot|comment|call|'
        'client_msg|other","reason":"кратко по-русски",'
        '"regulation":{"ack":"yes|no|na","one_step":"yes|no|na","why":"yes|no|na",'
        '"risk":"yes|no|na","check_back":"yes|no|na","no_conveyor":"yes|no|na",'
        '"no_invented_promise":"yes|no|na"}}. Поле missing '
        "заполняй только для context_gap, иначе пустой строкой."
    )
    # Без вопроса клиента судья сравнивал два ответа в вакууме и не мог
    # отличить «бот ответил не на то» от «оператор перешёл к другой теме».
    question = (client_text or "").strip()
    parts = []
    if question:
        parts.append(f"Вопрос клиента:\n{question[:800]}")
    parts.append(f"Черновик ассистента:\n{(ai_answer or '')[:1200]}")
    after = after_context or {}
    if after.get("client_messages"):
        parts.append(
            "После черновика клиент дописал:\n"
            + "\n".join(f"— {m[:300]}" for m in after["client_messages"][:5])
        )
    if after.get("colleague_comments"):
        parts.append(
            "Комментарии коллег в тикете (ассистент их не видел):\n"
            + "\n".join(f"— {c[:300]}" for c in after["colleague_comments"][:5])
        )
    if after.get("attachment_names"):
        parts.append("Вложения в тикете: " + ", ".join(after["attachment_names"][:8]))
    if (photo_descriptions or "").strip():
        parts.append(f"Описание вложений:\n{photo_descriptions.strip()[:600]}")
    if (call_notes or "").strip():
        parts.append(f"Из звонка по этому тикету:\n{call_notes.strip()[:600]}")
    parts.append(f"Ответ оператора:\n{(reference or '')[:1200]}")
    return system, "\n\n".join(parts)


def _normalise_missing(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if value in _MISSING_CHANNELS else "other"


async def judge_divergence(
    ai_answer: str,
    reference: str,
    *,
    client_text: str = "",
    after_context: dict | None = None,
    photo_descriptions: str = "",
    call_notes: str = "",
    _call_fn=None,
) -> tuple[str, str] | None:
    """Категория расхождения от LLM. None при сбое/мусорном JSON — пара не судится.

    Для context_gap в reason приписывается `missing=<канал>`: по нему утренняя
    сводка показывает, какого именно контекста боту не хватает, — и это уже
    задача на канал, а не решение по факту.
    """
    if _call_fn is None:
        from ..ai_summary import call_groq_json as _call_fn
    from ..config import config

    system, user = _build_judge_prompt(
        ai_answer, reference, client_text,
        after_context=after_context,
        photo_descriptions=photo_descriptions,
        call_notes=call_notes,
    )
    try:
        raw = await _call_fn(system, user, model=config.agent_selfcheck_model)
        obj = _json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("category") not in _CATEGORIES:
        return None
    category = obj["category"]
    reason = str(obj.get("reason", ""))[:200]
    if category == "context_gap":
        reason = f"missing={_normalise_missing(obj.get('missing'))} {reason}".strip()
    reg = obj.get("regulation")
    if isinstance(reg, dict):
        compact = ",".join(
            f"{k}:{reg[k]}" for k in _REG_KEYS if str(reg.get(k, "")) in ("yes", "no", "na")
        )
        if compact:
            reason = f"{reason} reg={compact}".strip()
    return category, reason


async def reconcile_one(
    *, ai_answer: str, posts, anchor_post_id, staff: set[str],
    client_text: str = "", _judge_fn=None
) -> dict | None:
    """Сверка одного предложения. None, если сравнивать не с чем (нет ответа
    оператора / реплика оператора ответом не является) или судья не ответил."""
    if _judge_fn is None:
        _judge_fn = judge_divergence
    reference = find_operator_reply_after(posts, anchor_post_id, staff)
    if not reference:
        return None
    verdict = await _judge_fn(ai_answer, reference, client_text=client_text)
    if verdict is None:
        return None
    category, reason = verdict
    return {"reference_answer": reference, "category": category, "reason": reason}


async def reconcile_recent(
    *, hours: int = 24, _suggestions_fn=None, _posts_fn=None, _set_fn=None,
    _staff=None, _judge_fn=None, _sleep_fn=None, _candidate_fn=None, _topic_fn=None,
    pause_s: float = 4.0, retry_pause_s: float = 30.0,
) -> dict:
    """Ночная сверка свежих предложений с фактическими ответами операторов.

    Тянет посты через HDE API (под троттлом), судит LLM-судьёй, пишет
    judge_reference_answer + канонический judge_label (accepted/corrected) и
    категорию в judge_detail. not_comparable не размечается — только считается.
    Вердикт bot_wrong_fact дополнительно встаёт в очередь кандидатов в базу
    знаний. Пейсинг под Groq free-tier: pause_s между парами, один повтор после
    retry_pause_s. Ошибка по одному тикету не роняет проход.
    """
    from ..config import config

    if _suggestions_fn is None:
        from ..db import get_unjudged_suggestions as _suggestions_fn
    if _set_fn is None:
        from ..db import set_judge_result as _set_fn
    if _candidate_fn is None:
        from ..db import save_kb_candidate as _candidate_fn
    if _judge_fn is None:
        _judge_fn = judge_divergence
    if _topic_fn is None:
        from ..db import get_topic as _topic_fn
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

    stats = {"same_action": 0, "bot_better": 0, "bot_escalated": 0, "bot_wrong_fact": 0,
             "context_gap": 0, "not_comparable": 0, "skipped": 0, "errors": 0}
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
            client_text = sug.get("client_text") or ""
            after_context = collect_after_anchor(
                posts, sug.get("context_until_post_id"), staff,
                client_id=str(sug.get("client_id") or ""),
            )
            # Описания вложений и заметки после звонка живут на топике. Тикета
            # без топика (удалён, старый) достаточно, чтобы сверка встала, —
            # поэтому промах здесь не ошибка прохода, а пустой контекст.
            photo_descriptions = call_notes = ""
            try:
                topic = await _topic_fn(str(sug["ticket_id"]))
                if topic is not None:
                    photo_descriptions = getattr(topic, "photo_descriptions", "") or ""
                    call_notes = getattr(topic, "call_notes", "") or ""
            except Exception as exc:
                logger.debug(
                    "reconcile: topic lookup failed for %s: %s", sug.get("ticket_id"), exc
                )
            judge_kwargs = {
                "client_text": client_text,
                "after_context": after_context,
                "photo_descriptions": photo_descriptions,
                "call_notes": call_notes,
            }
            verdict = await _judge_fn(ai_answer, reference, **judge_kwargs)
            if verdict is None:
                await _sleep_fn(retry_pause_s)
                verdict = await _judge_fn(ai_answer, reference, **judge_kwargs)
            if verdict is None:
                stats["skipped"] += 1
                continue
            category, reason = verdict
            stats[category] += 1
            label = _CATEGORY_LABEL.get(category)
            if label is None and category != "context_gap":
                continue  # not_comparable: судить не по чему, метку не ставим
            # context_gap метку не получает, но вердикт пишется: иначе категория
            # не попадёт в сводку, и дырявый канал контекста останется невидимым.
            await _set_fn(
                sug["id"],
                reference_answer=reference,
                label=label,
                detail=f"judge:{category} {reason}".strip(),
                category=category,
            )
            if category == "bot_wrong_fact":
                # Неверный факт — либо в базе знаний нет статьи, либо она врёт.
                # Ставим в очередь; решает человек (категорию поставила модель).
                try:
                    await _candidate_fn(
                        suggestion_id=sug["id"],
                        ticket_id=str(sug["ticket_id"]),
                        title=sug.get("title") or "",
                        history=sug.get("history") or "",
                        ai_answer=ai_answer,
                        reference_answer=reference,
                        reason=reason,
                    )
                except Exception as exc:
                    logger.warning(
                        "reconcile: KB candidate queue failed for ticket %s: %s",
                        sug.get("ticket_id"), exc,
                    )
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("reconcile: ticket %s failed: %s", sug.get("ticket_id"), exc)
    return stats

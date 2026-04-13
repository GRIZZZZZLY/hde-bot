# AI Answer Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переработать AI-ответ с двух сообщений на три: Суть / Ответ клиенту / Памятка — с лимитом 3000 токенов.

**Architecture:** Единственный LLM-вызов возвращает три секции. `generate_ticket_summary` возвращает 4-элементный кортеж `(suit_line, client_line, memo_line, confidence_pct)`. Три последовательных Telegram-сообщения с разными кнопками.

**Tech Stack:** Python 3.10+, aiogram 3.x, aiohttp, aiosqlite, Groq API, Gemini API

---

## Файловая карта

| Файл | Что меняется |
|---|---|
| `bot/ai_summary.py` | `_FORMAT_INSTRUCTIONS`, `_build_system_prompt`, токены, парсинг, возврат 4-tuple |
| `bot/handlers/ai_feedback.py` | +`memo_feedback_kb()`, +`cb_memo_good`, +`cb_memo_bad` |
| `bot/topic_manager.py` | Распаковка 4-tuple, переименование сообщения 2, добавление сообщения 3 |

---

## Task 1: Обновить промпт, токены и парсинг в `bot/ai_summary.py`

**Files:**
- Modify: `bot/ai_summary.py`

- [ ] **Step 1: Заменить `_FORMAT_INSTRUCTIONS`**

Найти строки 100–108 в `bot/ai_summary.py`:
```python
_FORMAT_INSTRUCTIONS = (
    "Ответь РОВНО двумя строками — обе обязательны:\n"
    "Суть: <диагноз проблемы, бренд/модель если известны>\n"
    "Ответ: <конкретные технические шаги через →>\n\n"
    "Пример:\n"
    "Суть: АТОЛ 30Ф — ошибка связи с ОФД, истёк сертификат.\n"
    "Ответ: Меню ФН → Диагностика ОФД → Обновить сертификат в ЛК ОФД → Перерегистрация.\n\n"
    "ВАЖНО: шаги для специалиста, не для клиента. Не используй markdown. Не добавляй ничего лишнего."
)
```

Заменить на:
```python
_FORMAT_INSTRUCTIONS = (
    "Ответь РОВНО тремя строками — все три обязательны:\n"
    "Суть: <диагноз проблемы, бренд/модель если известны>\n"
    "Клиенту: <сообщение от лица поддержки — готовый ответ, инструкция или уточняющий вопрос>\n"
    "Памятка: <внутренний чеклист для специалиста: что проверить → как решить, шаги через →>\n\n"
    "Правила для «Клиенту»:\n"
    "- Вежливо, от лица поддержки, без технического жаргона\n"
    "- Если проблема ясна и решение известно → готовый ответ с шагами для клиента\n"
    "- Если нужна информация от клиента → уточняющий вопрос\n"
    "- Если клиент должен выполнить действия сам → пошаговая инструкция\n"
    "Правила для «Памятка»: технически точно, для специалиста, шаги через →\n"
    "Не используй markdown. Не добавляй ничего лишнего."
)
```

- [ ] **Step 2: Обновить `_build_system_prompt` — убрать фразу про специалиста**

Найти в `_build_system_prompt` (строки ~144–152):
```python
    base = (
        "Ты — помощник технического специалиста 2-й линии поддержки.\n"
        "Специализация: кассовое оборудование (АТОЛ, Эвотор, Штрих-М, Viki),\n"
        "фискальные регистраторы, ОФД/ФН, сетевые подключения, эквайринг\n"
        "(Сбер, ВТБ, Тинькофф).\n\n"
        "Тикет передан с 1-й линии — базовую диагностику уже провели.\n\n"
        "ВАЖНО: ты подсказываешь СПЕЦИАЛИСТУ что делать, не пишешь ответ клиенту.\n"
        "Ответ — техническая инструкция к выполнению.\n\n"
    )
```

Заменить на:
```python
    base = (
        "Ты — помощник технического специалиста 2-й линии поддержки кассового оборудования.\n"
        "Специализация: АТОЛ, Эвотор, Штрих-М, Viki, фискальные регистраторы, ОФД/ФН,\n"
        "сетевые подключения, эквайринг (Сбер, ВТБ, Т-Банк).\n\n"
        "Тикет передан с 1-й линии — базовую диагностику уже провели.\n\n"
    )
```

- [ ] **Step 3: Увеличить лимит токенов Groq 2000 → 3000**

Найти в `_call_groq_for_summary` (строка ~73):
```python
                    "max_tokens": 2000,
```
Заменить на:
```python
                    "max_tokens": 3000,
```

- [ ] **Step 4: Увеличить лимит токенов Gemini 2000 → 3000**

Найти в `generate_ticket_summary` (строка ~434):
```python
                    "generationConfig": {
                        "temperature": 0.3,
                        "maxOutputTokens": 2000,
                    },
```
Заменить на:
```python
                    "generationConfig": {
                        "temperature": 0.3,
                        "maxOutputTokens": 3000,
                    },
```

- [ ] **Step 5: Обновить сигнатуру возвращаемого значения**

Найти строку ~332:
```python
) -> tuple[str, str, int] | None:
    """Return (suit_line, answer_line, confidence_pct) or None if disabled/failed."""
```
Заменить на:
```python
) -> tuple[str, str, str, int] | None:
    """Return (suit_line, client_line, memo_line, confidence_pct) or None if disabled/failed."""
```

- [ ] **Step 6: Обновить парсер — добавить секцию `Клиенту:` и `Памятка:`**

Найти блок парсинга (строки ~493–519):
```python
    # Parse "Суть: ...\nОтвет: ..."
    import re as _re
    logger.info("Gemini raw response for ticket %s: %r", ticket_id, text[:400])
    suit_line = ""
    answer_line = ""
    current_key: str | None = None
    for line in text.splitlines():
        # Strip markdown bold/italic (**text**, *text*) before matching
        cleaned = _re.sub(r"\*+", "", line).strip()
        lower = cleaned.lower()
        if lower.startswith("суть:"):
            suit_line = cleaned[5:].strip()
            current_key = "suit"
        elif lower.startswith("ответ:"):
            answer_line = cleaned[6:].strip()
            current_key = "answer"
        elif cleaned and current_key == "suit" and not suit_line:
            suit_line = cleaned  # content on next line after "Суть:"
        elif cleaned and current_key == "answer" and not answer_line:
            answer_line = cleaned  # content on next line after "Ответ:"
        elif not cleaned:
            current_key = None  # blank line resets context

    if not suit_line and not answer_line:
        logger.warning("Could not parse Суть/Ответ from Gemini response for ticket %s", ticket_id)
        return None

    return (suit_line, answer_line, confidence_pct)
```

Заменить на:
```python
    # Parse "Суть: ...\nКлиенту: ...\nПамятка: ..."
    import re as _re
    logger.info("AI raw response for ticket %s: %r", ticket_id, text[:400])
    suit_line = ""
    client_line = ""
    memo_line = ""
    current_key: str | None = None
    for line in text.splitlines():
        # Strip markdown bold/italic (**text**, *text*) before matching
        cleaned = _re.sub(r"\*+", "", line).strip()
        lower = cleaned.lower()
        if lower.startswith("суть:"):
            suit_line = cleaned[5:].strip()
            current_key = "suit"
        elif lower.startswith("клиенту:"):
            client_line = cleaned[8:].strip()
            current_key = "client"
        elif lower.startswith("памятка:"):
            memo_line = cleaned[8:].strip()
            current_key = "memo"
        # Legacy fallback: support old "Ответ:" label
        elif lower.startswith("ответ:"):
            client_line = cleaned[6:].strip()
            current_key = "client"
        elif cleaned and current_key == "suit" and not suit_line:
            suit_line = cleaned
        elif cleaned and current_key == "client" and not client_line:
            client_line = cleaned
        elif cleaned and current_key == "memo" and not memo_line:
            memo_line = cleaned
        elif not cleaned:
            current_key = None  # blank line resets context

    if not suit_line and not client_line:
        logger.warning("Could not parse Суть/Клиенту from AI response for ticket %s", ticket_id)
        return None

    return (suit_line, client_line, memo_line, confidence_pct)
```

- [ ] **Step 7: Commit**

```bash
git add bot/ai_summary.py
git commit -m "feat: update AI prompt to 3 sections (Суть/Клиенту/Памятка), 3000 tokens"
```

---

## Task 2: Добавить `memo_feedback_kb` и callbacks в `bot/handlers/ai_feedback.py`

**Files:**
- Modify: `bot/handlers/ai_feedback.py`

- [ ] **Step 1: Добавить функцию `memo_feedback_kb` после `answer_feedback_kb`**

Найти строку после закрывающей скобки `answer_feedback_kb` (~строка 53):
```python
    ])


async def register_feedback_pending(
```

Вставить перед `async def register_feedback_pending`:
```python

def memo_feedback_kb() -> InlineKeyboardMarkup:
    """Keyboard for the Памятка message."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Полезно", callback_data="memo:good"),
        InlineKeyboardButton(text="👎 Бесполезно", callback_data="memo:bad"),
    ]])

```

- [ ] **Step 2: Добавить callbacks `cb_memo_good` и `cb_memo_bad` в конец файла**

Добавить после `cb_suit_bad` (~строка 186):
```python

@router.callback_query(F.data == "memo:good")
async def cb_memo_good(callback: CallbackQuery) -> None:
    await callback.answer("👍 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "memo:bad")
async def cb_memo_bad(callback: CallbackQuery) -> None:
    await callback.answer("👎 Отмечено", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
```

- [ ] **Step 3: Commit**

```bash
git add bot/handlers/ai_feedback.py
git commit -m "feat: add memo_feedback_kb and memo:good/bad callbacks"
```

---

## Task 3: Отправлять три сообщения в `bot/topic_manager.py`

**Files:**
- Modify: `bot/topic_manager.py`

- [ ] **Step 1: Обновить импорт — добавить `memo_feedback_kb`**

Найти (~строка 287):
```python
    from .handlers.ai_feedback import suit_feedback_kb, answer_feedback_kb, register_feedback_pending
```
Заменить на:
```python
    from .handlers.ai_feedback import suit_feedback_kb, answer_feedback_kb, memo_feedback_kb, register_feedback_pending
```

- [ ] **Step 2: Распаковать 4-tuple вместо 3-tuple**

Найти (~строка 320):
```python
    suit_line, answer_line, confidence_pct = result
```
Заменить на:
```python
    suit_line, client_line, memo_line, confidence_pct = result
```

- [ ] **Step 3: Переименовать сообщение 2 и добавить сообщение 3**

Найти блок отправки сообщений (~строки 337–357):
```python
        # Message 2 — Предложенный ответ
        if answer_line:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=(
                    f"💡 <b>Предложенный ответ:</b>\n"
                    f"<i>«{_html_escape(answer_line)}»</i>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=answer_feedback_kb(),
            )
        plain_history = _build_history_text(posts, info)
        await register_feedback_pending(
            topic_id=topic_id,
            ticket_id=ticket_id,
            history=plain_history,
            title=ticket_title,
            answer_text=answer_line,
        )
```

Заменить на:
```python
        # Message 2 — Ответ клиенту
        if client_line:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=(
                    f"💬 <b>Ответ клиенту:</b>\n"
                    f"<i>«{_html_escape(client_line)}»</i>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=answer_feedback_kb(),
            )
        # Message 3 — Памятка для специалиста
        if memo_line:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=(
                    f"📋 <b>Памятка:</b>\n"
                    f"{_html_escape(memo_line)}"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=memo_feedback_kb(),
            )
        plain_history = _build_history_text(posts, info)
        await register_feedback_pending(
            topic_id=topic_id,
            ticket_id=ticket_id,
            history=plain_history,
            title=ticket_title,
            answer_text=client_line,
        )
```

- [ ] **Step 4: Commit**

```bash
git add bot/topic_manager.py
git commit -m "feat: send 3 AI messages — Суть / Ответ клиенту / Памятка"
```

---

## Task 4: Push и деплой

- [ ] **Step 1: Push**

```bash
git push
```

- [ ] **Step 2: Деплой на сервере**

```bash
cd /opt/hde-bot && git pull && systemctl restart hde-bot
```

- [ ] **Step 3: Проверка**

Назначить тикет на себя в HDE → в Telegram должны появиться три сообщения:
1. `🧠 Суть (XX%): ...` + кнопки `[👍 Верно] [👎 Неверно]`
2. `💬 Ответ клиенту: «...»` + кнопки `[👍] [✏️] [👎]` + `[📤 Ответить клиенту] [💬 Комментарий]`
3. `📋 Памятка: ...` + кнопки `[👍 Полезно] [👎 Бесполезно]`

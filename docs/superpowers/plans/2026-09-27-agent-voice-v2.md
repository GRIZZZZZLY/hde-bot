# Агент черновиков v2 — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** черновик ответа клиенту по регламенту «Лояльная техподдержка», который
владелец отправляет почти без правок. Черновик приходит сам на первое сообщение
тикета и на каждый ответ клиента.

**Architecture:** новый путь включается флагом `AGENT_VOICE_V2_ENABLED` поверх
существующего агента (`bot/agent/`):
- один вызов qwen с коротким промптом (≤ ~6 тыс. токенов на запрос);
- голос по регламенту в одном файле `bot/prompts/voice_ru.md`;
- проверки кодом (`bot/agent/lint.py`) вместо LLM self-check;
- черновик одним сообщением, на ответах клиента — правкой того же сообщения.

Старый путь остаётся нетронутым при выключенном флаге.

**Tech Stack:** Python 3.13, aiogram 3, aiohttp, SQLite (aiosqlite), Groq
(`qwen/qwen3.8-27b`), pytest + pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-09-27-agent-voice-v2-design.md` (читать
вместе с этим планом). Разбор с данными: `docs/agent-review-2026-09-27.md`.

## Global Constraints

- Groq `qwen/qwen3.8-27b`: 8000 TPM, 1000 RPD. Запрос = промпт + `max_tokens`.
  Системный промпт v2 плюс история ≤ 11 500 символов, `max_tokens=600`.
- Кэширования промпта для qwen нет. Экономить можно только длиной.
- Всё новое поведение — только при `config.agent_voice_v2_enabled=True`. При
  False существующие тесты проходят без правок, кроме явно оговорённых в
  задачах.
- Бот ничего не отправляет клиенту без кнопки оператора.
- Регламент `docs/Лояльная техническая поддержка — anti-slop.md` побеждает любые
  другие стайлгайды.
- Схему БД не меняем: результат проверок и `analysis` пишутся JSON в
  `ai_suggestions.self_check`.
- Тексты комментариев, коммитов и документов — нормальным русским или английским
  языком. Пути хранилища заметок и URL сессий в репозиторий не пишем.
- В конце каждой задачи: `python -m pytest tests/ -q` зелёный. Сейчас в наборе
  901 тест.
- Отклонение от spec §6, согласовано при планировании. Если ответ клиента плюс
  черновик больше 4096 символов, черновик уходит отдельным тихим сообщением.
  Ответ клиента не обрезается: резать уже собранный HTML рискованно.

## Review Focus

1. **Гонка черновиков.** Два ответа клиента подряд, черновик на первый готов
   позже второго. Ожидание: у старого сообщения нет кнопок 📤, отправить
   устаревший черновик нельзя. Тест — в Task 6.
2. **Черновик не помещается в сообщение** (длинный ответ клиента плюс черновик
   больше 4096 символов). Ожидание: черновик уходит отдельным тихим
   сообщением, исходное сообщение не ломается. Тест — в Task 6.
3. **HTML в тексте модели** (`<`, `&` в черновике или памятке). Ожидание:
   Telegram не отвечает «can't parse entities», всё экранировано. Тест — в
   Task 6.
4. **Последнее сообщение клиента — телефон, ID или «Хорошо, спасибо, ждём».**
   Ожидание: поиск идёт по первому содержательному сообщению, черновик не
   отвечает на «спасибо». Тест — в Task 2.
5. **Пароль в Памятке в формате «пароль: abc123» и «ID 123 456, пароль tudiuk».**
   Ожидание: в Telegram уходит «•••», слово «пароль» в тексте для клиента не
   трогается. Тест — в Task 3.

---

## Карта файлов

| Файл | Ответственность |
|---|---|
| `bot/prompts/voice_ru.md` (new) | голос по регламенту — единственный источник правил для текста клиенту |
| `bot/prompts/voice_examples.json` (new) | 6 эталонных примеров «ситуация → ответ» |
| `bot/agent/voice.py` (new) | сборка системного промпта v2 из блоков |
| `bot/agent/context_v2.py` (new) | чистые помощники контекста: запрос к поиску, чистка истории, лучший кусок статьи, сигналы `stress` / `first_staff_reply` |
| `bot/agent/lint.py` (new) | проверки черновика кодом |
| `bot/agent/context.py` | ветка v2 в `build_agent_context` |
| `bot/agent/generate.py` | ветка v2 в `generate_agent_draft` |
| `bot/agent/actions.py` | `parse_agent_draft`: `analysis`, `source_ids` |
| `bot/agent/pipeline.py` | ветка v2: без self-check, с lint |
| `bot/ai_summary.py` | `prompt_version_tag()` → `voice-v2` |
| `bot/topic_manager.py` | без legacy-фолбэка при v2; авто-черновик на ответ клиента |
| `bot/topic_history.py` | одно сообщение-черновик; `append_draft_to_reply`; кнопка на новом тикете |
| `bot/formatter.py` | `format_draft_block` |
| `bot/handlers/ai_feedback.py` | `draft_kb()` |
| `bot/config.py`, `.env.example` | 3 новых флага |
| `bot/agent/reconcile.py`, `bot/scheduler.py` | судья сверки и разделение флагов |
| `bot/optimizer/judge.py` | `voice_ru.md` вместо `style_guide_ru.md` |
| `scripts/eval_voice.py` (new) | набор, прогон старый/новый, счётчики, выгрузка A/B |
| `.claude/skills/anti-slop-ru/**` (new) | скилл с жанром «поддержка» |

---

### Task 1: Флаги, голос по регламенту и сборка промпта v2

**Files:**
- Create: `bot/prompts/voice_ru.md`, `bot/prompts/voice_examples.json`, `bot/agent/voice.py`, `tests/test_voice_prompt.py`
- Modify: `bot/config.py` (поля + парсинг рядом с `nightly_reconcile_enabled`), `.env.example`
- Add to git: `docs/Лояльная техническая поддержка — anti-slop.md` (исходник регламента, сейчас не в git)

**Interfaces:**
- Produces:
  - `config.agent_voice_v2_enabled: bool`, `config.agent_reply_drafts_enabled: bool`,
    `config.reconcile_kb_distill_enabled: bool` (все по умолчанию False);
  - `bot.agent.voice.build_prompt(context: dict, ticket_title: str) -> str`;
  - `bot.agent.voice.load_voice() -> str`;
  - `bot.agent.voice.ALLOWED_URLS: tuple[str, ...]`;
  - `bot.agent.voice.SYSTEM_BUDGET_CHARS = 8500`.
- Контракт `context` для `build_prompt` (ключи, которые читаются):
  `ticket_facts`, `attachments`, `call_notes` (str), `evidence` (list[dict] с
  `source_type`, `source_id`, `used_excerpt`), `demos` (list[dict] с
  `source_id`, `used_excerpt`), `stress` (bool), `first_staff_reply` (bool).
  Отсутствующие ключи считаются пустыми.

- [ ] **Step 1: Флаги в конфиге**

В `bot/config.py` в dataclass рядом с `nightly_reconcile_enabled: bool` добавить:

```python
    agent_voice_v2_enabled: bool
    agent_reply_drafts_enabled: bool
    reconcile_kb_distill_enabled: bool
```

и в `from_env` рядом с `nightly_reconcile_enabled=...`:

```python
            # Агент черновиков v2 (spec 2026-09-27): короткий промпт по регламенту,
            # проверки кодом вместо self-check, одно сообщение-черновик.
            agent_voice_v2_enabled=_parse_bool(
                os.getenv("AGENT_VOICE_V2_ENABLED"), default=False
            ),
            # Черновик на каждый новый ответ клиента (дописывается в его сообщение).
            agent_reply_drafts_enabled=_parse_bool(
                os.getenv("AGENT_REPLY_DRAFTS_ENABLED"), default=False
            ),
            # Пополнение базы знаний после ночной сверки. Отдельно от самой сверки:
            # замер включён, а база сама не пополняется (решение тихого режима).
            reconcile_kb_distill_enabled=_parse_bool(
                os.getenv("RECONCILE_KB_DISTILL_ENABLED"), default=False
            ),
```

В `.env.example` рядом с `NIGHTLY_RECONCILE_ENABLED` добавить:

```
# Агент черновиков v2: промпт по регламенту «Лояльная техподдержка»
AGENT_VOICE_V2_ENABLED=0
# Черновик на каждый ответ клиента (нужен AGENT_VOICE_V2_ENABLED=1)
AGENT_REPLY_DRAFTS_ENABLED=0
# Пополнять базу знаний по итогам ночной сверки
RECONCILE_KB_DISTILL_ENABLED=0
```

- [ ] **Step 2: Файл голоса `bot/prompts/voice_ru.md`**

```markdown
ГОЛОС ОТВЕТА КЛИЕНТУ (поле client)
Пиши как оператор, к которому клиент хочет вернуться: коротко, по делу, на «вы», живо.

Части ответа — только нужные, в этом порядке:
1) Признание — ТОЛЬКО если ниже стоит «Сигнал: стресс». Одна короткая фраза, которая
   называет конкретное неудобство («смену надо закрыть», «очередь ждёт»), и сразу к делу.
2) Риск — если шаг прервёт продажи или может потерять несохранённые данные: скажи ДО шага.
3) Шаг ИЛИ один вопрос. Шаг — одно действие или связка, которую делают за раз. Сначала
   быстрый обходной путь из источников и истории; удалёнка — только если без неё нельзя.
   Вопрос — один, прямой. Просишь фото, номер или доступ — сразу скажи зачем
   («так я увижу, на каком шаге зависает»).
4) Проверка — после инструкции коротко попроси проверить результат, формулировку меняй
   («Подскажите, получилось?», «Проверьте, пожалуйста, сейчас»).
Обычно 1–2 предложения, максимум 3 и около 50 слов. Предложение без одной из частей удали.

Нельзя:
- приветствие в идущем диалоге («Добрый день» — только если ниже «Первый ответ: да»),
  прощания, «спасибо за обращение», «обращайтесь», «если возникнут вопросы»;
- извинение без действия; «в кратчайшие сроки», «на текущий момент», «данный»,
  «осуществить», «в рамках», «необходимо выполнить»;
- вопросы-обвинения («вы точно подключили?») — вместо них совместная проверка
  («пришлите фото разъёма, посмотрим»);
- обещания за других и сроки, которых нет в истории («инженер свяжется», «в понедельник»);
- прошедшее время о несделанном («подключился», «настроил»). Можно: «пришлите номер —
  я подключусь»;
- «не просто X, а Y», перечисления из трёх пунктов, общие слова вместо конкретики.
Называй всё так, как клиент видит на экране: «номер рабочего места» в AnyDesk,
«Боковое меню → Настройки → Фискальное устройство». «Пожалуйста» — не больше одного раза.
Если проблема на стороне банка, провайдера или прошивки — скажи прямо, где она. Срок
называй, только если он известен. Если есть обходной путь — дай его.
```

- [ ] **Step 3: Эталонные примеры `bot/prompts/voice_examples.json`**

Суть взята из реальных ответов операторов (тикеты 953, 1090, 1116, 881), форма —
по регламенту. Владелец утверждает эти тексты на ревью плана.

```json
{
  "examples": [
    {"situation": "Viki Print + Posiflora, касса не печатает. Клиент: «9:00 и где ваш сотрудник я должна смену закрыть». Сигнал: стресс.",
     "client": "Понимаю, смену надо закрыть сейчас, давайте быстро. Переключите кабель кассы в другой USB-разъём на компьютере и перезагрузите кассу кнопкой питания. Подскажите, получилось?"},
    {"situation": "Атол + Viki Print, ошибка закрытия смены, клиент уже прислал доступ. Клиент: «Хорошо, спасибо, ждём».",
     "client": "Подключите, пожалуйста, кассу в другой USB-разъём и попробуйте закрыть смену ещё раз. Получилось?"},
    {"situation": "Сбер-терминал, оплата не проходит, роутер и терминал уже перезагружали, удалёнки в переписке не было.",
     "client": "Чтобы увидеть, что происходит с оплатой, мне нужно подключиться к компьютеру. Скачайте AnyDesk: https://anydesk.com/ru, запустите и пришлите номер рабочего места — я подключусь."},
    {"situation": "Posiflora, при завершении заказа всплывает ошибка. Клиент: «При завершении заказа вот это вылазит. Как быть?»",
     "client": "Закройте программу Posiflora полностью, откройте снова и попробуйте провести заказ ещё раз. Подскажите, получилось?"},
    {"situation": "Первое сообщение: «Не печатает чек». Модель кассы и текст ошибки неизвестны. Первый ответ: да.",
     "client": "Добрый день. Пришлите, пожалуйста, фото экрана кассы в момент ошибки — так я увижу, на каком шаге останавливается печать."},
    {"situation": "Клиент: «После переезда касса не видит принтер». Как подключён принтер, неизвестно.",
     "client": "Подскажите, как сейчас подключён принтер — по USB или по Wi-Fi? От этого зависит, что проверять первым."}
  ]
}
```

- [ ] **Step 4: Написать падающий тест `tests/test_voice_prompt.py`**

```python
"""Промпт v2: бюджет, порядок блоков, сигналы (spec 2026-09-27 §4)."""
from bot.agent import voice


def _worst_context() -> dict:
    return {
        "ticket_facts": "Компания клиента: " + "Ц" * 300,
        "attachments": "А" * 900,
        "call_notes": "З" * 900,
        "evidence": [
            {"source_type": "knowledge_item", "source_id": 1, "used_excerpt": "К" * 900},
            {"source_type": "knowledge_item", "source_id": 2, "used_excerpt": "К" * 900},
        ],
        "demos": [
            {"source_type": "dialogue_pair", "source_id": 7, "used_excerpt": "П" * 500},
            {"source_type": "dialogue_pair", "source_id": 8, "used_excerpt": "П" * 500},
        ],
        "stress": True,
        "first_staff_reply": True,
    }


def test_worst_case_fits_budget_with_full_history():
    system = voice.build_prompt(_worst_context(), "Т" * 200)
    history_budget = 3000
    assert len(system) + history_budget <= 11_500, len(system)
    assert len(system) <= voice.SYSTEM_BUDGET_CHARS


def test_voice_comes_before_ticket_data_and_format_is_last():
    system = voice.build_prompt(_worst_context(), "Касса не печатает")
    assert system.index("ГОЛОС ОТВЕТА КЛИЕНТУ") < system.index("Источники")
    assert system.rstrip().endswith("}")          # формат JSON — последним
    assert '"analysis"' in system


def test_signals_are_rendered_only_when_set():
    calm = voice.build_prompt({"stress": False, "first_staff_reply": False}, "T")
    assert "Сигнал: стресс" not in calm
    assert "Первый ответ: нет" in calm
    tense = voice.build_prompt({"stress": True, "first_staff_reply": True}, "T")
    assert "Сигнал: стресс" in tense
    assert "Первый ответ: да" in tense


def test_sources_are_labelled_with_the_ids_lint_expects():
    system = voice.build_prompt(_worst_context(), "T")
    assert "[KB#1]" in system and "[пара#7]" in system


def test_old_style_rules_do_not_leak_in():
    system = voice.build_prompt({}, "T")
    assert "≤20 слов" not in system
    assert "номер рабочего места" in system        # регламент: как на экране AnyDesk
```

- [ ] **Step 5: Запустить — должен упасть**

Run: `python -m pytest tests/test_voice_prompt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.agent.voice'`.

- [ ] **Step 6: Реализация `bot/agent/voice.py`**

```python
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

SYSTEM_BUDGET_CHARS = 8500
ALLOWED_URLS = ("https://anydesk.com/ru", "https://rudesktop.ru/downloads/")

_FACTS_LIMIT = 400
_SIDE_LIMIT = 350          # описание вложений и заметки звонка — каждое
_SOURCE_LIMIT = 700        # один кусок статьи
_DEMO_LIMIT = 300          # один прошлый ответ

_ROLE = (
    "Ты помогаешь инженеру 2-й линии поддержки: кассы (АТОЛ, Эвотор, Штрих-М, Viki), "
    "Posiflora, эквайринг. Пишешь черновик ответа клиенту, оператор отправит его сам. "
    "Быстрый шаг, который клиент сделает сам, лучше удалёнки."
)

_ACTION_RULES = (
    "ВЫБОР ДЕЙСТВИЯ (поле action):\n"
    "- ANSWER — в источниках или истории есть шаг: дай его;\n"
    "- ASK — данных не хватает: ровно один вопрос и зачем он нужен;\n"
    "- ESCALATE — деньги, фискальные изменения, необратимые действия, доступы: client пустой;\n"
    "- NO_ACTION — клиент не задал вопроса (благодарность, «ок»): client пустой.\n"
    "Опоры нет ни в источниках, ни в истории — ASK, шаг не выдумывай. "
    "Удалёнку предлагай, только если шага нет. Ссылки: AnyDesk https://anydesk.com/ru, "
    "RuDesktop https://rudesktop.ru/downloads/. Не знаешь, какая программа стоит у клиента, — спроси.\n"
    "В source_ids перечисли метки источников ([KB#…], [пара#…]), на которые опирается шаг."
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
        _SUIT_MEMO_RULES,
        _examples_block(),
        _ticket_block(context or {}, ticket_title),
        _FORMAT,
    ])
```

- [ ] **Step 7: Запустить — должен пройти**

Run: `python -m pytest tests/test_voice_prompt.py -q`
Expected: 5 passed.

Если бюджетный тест упал — сокращать тексты `voice_ru.md` и примеров, а не
поднимать `SYSTEM_BUDGET_CHARS`: лимит следует из 8000 TPM.

- [ ] **Step 8: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → всё зелёное.

```bash
git add bot/config.py .env.example bot/prompts/voice_ru.md bot/prompts/voice_examples.json bot/agent/voice.py tests/test_voice_prompt.py "docs/Лояльная техническая поддержка — anti-slop.md"
git commit -m "feat(agent): the draft voice lives in one file, built from the loyal-support regulation"
```

---

### Task 2: Контекст v2 — поиск по сути, история без макросов, куски статей, сигналы

**Files:**
- Create: `bot/agent/context_v2.py`, `tests/test_agent_context_v2.py`
- Modify: `bot/agent/context.py` (`build_agent_context`, ветка по флагу)

**Interfaces:**
- Consumes: `config.agent_voice_v2_enabled` (Task 1),
  `operator_text.BOILERPLATE_RE`, `clean_operator_text`, `strip_html`;
  `knowledge.chunker.chunk_markdown(text, target, overlap)`.
- Produces (в `bot/agent/context_v2.py`):
  - `is_substantive(text: str) -> bool`;
  - `client_messages(posts, client_id) -> list[str]`;
  - `build_retrieval_query(title: str, messages: list[str]) -> str`;
  - `strip_history_macros(history: str) -> str`;
  - `best_chunk(content: str, query: str, target: int = 700) -> str`;
  - `detect_stress(messages: list[str]) -> bool`;
  - `is_first_staff_reply(posts, client_id) -> bool`;
  - `clean_demo_answer(text: str) -> str`.
- При v2 `build_agent_context` возвращает те же ключи плюс `stress: bool`,
  `first_staff_reply: bool`. `history` уже без макросов. KB-фрагменты — лучший
  кусок до 700 символов. Демо — максимум 2.

- [ ] **Step 1: Падающие тесты `tests/test_agent_context_v2.py`**

```python
from types import SimpleNamespace

import numpy as np

from bot.agent import context_v2 as cv2
from bot.agent.context import build_agent_context
from bot.config import config


def _p(pid, uid, text, is_comment=False):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text, is_comment=is_comment,
                           date_created=f"10:00:0{pid} 01.01.2026")


def test_phone_ack_and_ids_are_not_substantive():
    for t in ["+7 913 674 89 16", "Хорошо, спасибо ждем", "Да, все верно",
              "1336770727 ани деск", "", "ок"]:
        assert not cv2.is_substantive(t), t
    assert cv2.is_substantive("Касса не печатает чек после обновления")


def test_query_uses_first_and_last_substantive_messages():
    msgs = ["Касса Атол не печатает чек после обновления", "Выдаёт ошибку порт недоступен",
            "+7 913 674 89 16", "Хорошо, спасибо ждем"]
    q = cv2.build_retrieval_query("Не печатает", msgs)
    assert "не печатает чек" in q.lower()
    assert "порт недоступен" in q
    assert "913" not in q and "спасибо" not in q
    assert len(q) <= 600


def test_query_falls_back_to_title_when_nothing_substantive():
    assert cv2.build_retrieval_query("Тема", ["89295848404", "ок"]).strip() == "Тема"


def test_history_drops_escalation_macro_and_compresses_anydesk_instruction():
    history = (
        "Клиент: касса не печатает\n"
        "Сотрудник: Ваше обращение принято в работу и передано профильному специалисту\n"
        "Сотрудник: Необходимо удаленно подключиться к вашему компьютеру. Скачайте программу "
        "для удаленного доступа AnyDesk по ссылке https://anydesk.com/ru\n"
        "Сотрудник: Перезагрузите кассу кнопкой питания\n"
        "Коллега: клиент на взводе"
    )
    out = cv2.strip_history_macros(history)
    assert "передано профильному специалисту" not in out
    assert "Скачайте программу" not in out
    assert "[ранее предложено удалённое подключение]" in out
    assert "Перезагрузите кассу кнопкой питания" in out
    assert "Коллега: клиент на взводе" in out
    assert out.startswith("Клиент: касса не печатает")


def test_best_chunk_picks_the_part_with_query_words():
    article = ("Введение про компанию и историю продукта. " * 20 + "\n\n"
               + "Чтобы касса печатала чек, откройте Настройки → Фискальное устройство и "
               "выберите порт COM3. " * 3)
    chunk = cv2.best_chunk(article, "касса не печатает чек фискальное устройство", target=300)
    assert "Фискальное устройство" in chunk
    assert len(chunk) <= 300 * 1.5 + 50


def test_best_chunk_returns_short_content_as_is():
    assert cv2.best_chunk("коротко", "запрос") == "коротко"


def test_stress_markers():
    assert cv2.detect_stress(["У меня опять касса зависла!"])
    assert cv2.detect_stress(["очередь стоит, а вы молчите"])
    assert cv2.detect_stress(["9:00 и где ваш сотрудник я должна смену закрыть"])
    assert not cv2.detect_stress(["Подскажите, как добавить товар в номенклатуру?"])


def test_first_staff_reply_ignores_bot_boilerplate():
    posts = [_p(1, 1, "не печатает"),
             _p(2, 99, "Ваше обращение принято в работу и передано специалисту")]
    assert cv2.is_first_staff_reply(posts, 1)
    posts.append(_p(3, 99, "Перезагрузите кассу, пожалуйста"))
    assert not cv2.is_first_staff_reply(posts, 1)


def test_demo_answer_is_cleaned_of_greeting_and_closer():
    raw = "Добрый день! Перезагрузите роутер и терминал. Всегда рад помочь!"
    assert cv2.clean_demo_answer(raw) == "Перезагрузите роутер и терминал."


async def test_v2_context_has_signals_and_clean_history(monkeypatch):
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    monkeypatch.setattr(config, "agent_dynamic_fewshot_enabled", False)
    posts = [_p(1, 1, "Опять касса Атол не печатает чек, очередь!"),
             _p(2, 99, "Обращение принято в работу и передано специалисту"),
             _p(3, 1, "+7 913 674 89 16")]
    info = SimpleNamespace(client_id=1)
    seen = {}

    async def fake_embed(text, task_type="query"):
        seen["query"] = text
        return np.ones(4, dtype=np.float32)

    async def fake_similar(emb, *, limit=3, query_text="", company_id=""):
        item = SimpleNamespace(id=12, url=None, title="t",
                               content="Вступление. " * 100 + "\n\nАтол не печатает чек: смените порт.")
        return [(item, 0.9)]

    async def none_async(*a, **k):
        return None

    ctx = await build_agent_context(
        posts, info, "Не печатает", _embed_fn=fake_embed, _similar_fn=fake_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_async, _pattern_fn=none_async,
        _topic_fn=none_async,
    )
    assert "913" not in seen["query"]
    assert ctx["stress"] is True
    assert ctx["first_staff_reply"] is True
    assert "передано специалисту" not in ctx["history"]
    assert "смените порт" in ctx["evidence"][0]["used_excerpt"]
    assert len(ctx["evidence"][0]["used_excerpt"]) <= 760
```

- [ ] **Step 2: Запустить — упадёт**

Run: `python -m pytest tests/test_agent_context_v2.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.agent.context_v2'`.

- [ ] **Step 3: Реализация `bot/agent/context_v2.py`**

```python
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

_STRESS_RE = re.compile(
    r"опять|снова|вчера уже|до сих пор|который раз|очеред|закрыть смену|смену закрыть|"
    r"не (?:можем|могу) (?:продавать|пробить|работать)|клиенты ждут|стоим|срочно|"
    r"!!|где ваш|сколько можно|жду уже|никто не (?:отвечает|ответил)",
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
```

Если `test_first_staff_reply_ignores_bot_boilerplate` не проходит из-за того,
что `clean_operator_text` не режет «Ваше обращение принято в работу и передано
специалисту»: проверить `BOILERPLATE_RE` — «принят[оа] в работу» там уже есть.
Регулярки не ослаблять.

- [ ] **Step 4: Ветка v2 в `bot/agent/context.py`**

В `build_agent_context` после вычисления `history` и `client_text` (строки
132–135) заменить формирование `retrieval_query` и добавить сигналы:

```python
    from ..config import config
    v2 = config.agent_voice_v2_enabled
    if v2:
        from . import context_v2 as cv2
        messages = cv2.client_messages(posts, getattr(info, "client_id", ""))
        history = cv2.strip_history_macros(history)
        retrieval_query = cv2.build_retrieval_query(ticket_title, messages)
    else:
        retrieval_query = f"{ticket_title}\n{client_text}"[:600]
```

Удалить старую строку `retrieval_query = f"{ticket_title}\n{client_text}"[:600]`.
Импорт `from ..config import config` ниже по функции (строка 177) убрать, он
теперь выше.

В цикле по `results` вместо `limit=3` использовать `limit=2 if v2 else 3`, а
`used_excerpt` строить так:

```python
            excerpt = (
                cv2.best_chunk(item.content or "", retrieval_query)
                if v2 else (item.content or "")[:_EXCERPT_LIMIT]
            )
            url = getattr(item, "url", None)
            evidence.append({
                "source_type": "knowledge_item",
                "source_id": item.id,
                "rank": rank,
                "score": round(float(score), 4),
                "title": getattr(item, "title", None),
                "used_excerpt": f"Статья: {url}\n{excerpt}" if url else excerpt,
            })
```

(`_excerpt_with_url` остаётся для старого пути: при `not v2` поведение прежнее.)

В блоке dynamic few-shot: `limit=2 if v2 else 3`, и при v2 брать
`cv2.clean_demo_answer(hit["operator_answer"])[:300]` вместо
`hit["operator_answer"][:400]`.

В возвращаемый словарь добавить:

```python
        "stress": cv2.detect_stress(messages) if v2 else False,
        "first_staff_reply": (
            cv2.is_first_staff_reply(posts, getattr(info, "client_id", "")) if v2 else False
        ),
```

- [ ] **Step 5: Прогнать новые и старые тесты контекста**

Run: `python -m pytest tests/test_agent_context_v2.py tests/test_agent_context.py tests/test_agent_ticket_context.py -q`
Expected: всё проходит. Старые тесты идут без флага, путь прежний.

- [ ] **Step 6: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/agent/context_v2.py bot/agent/context.py tests/test_agent_context_v2.py
git commit -m "feat(agent): the draft searches by the client's problem, not by their phone number"
```

---

### Task 3: Проверки черновика кодом (`bot/agent/lint.py`)

**Files:**
- Create: `bot/agent/lint.py`, `tests/test_agent_lint.py`

**Interfaces:**
- Consumes: `operator_text.CLOSER_RE`, `voice.ALLOWED_URLS` (Task 1).
- Produces:

```python
@dataclass
class LintResult:
    client: str
    memo: str
    fixed: list[str]
    hard: list[str]
    soft: list[str]
    def warning_line(self) -> str: ...      # "⚠️ Проверь: <hard[0]>" или ""
    def as_dict(self) -> dict: ...          # {"fixed": [...], "hard": [...], "soft": [...]}

def check_draft(client: str, memo: str, *, history: str, sources_text: str,
                facts: str = "", first_staff_reply: bool = False,
                grounds: list[str] | None = None,
                source_ids: list[str] | None = None) -> LintResult
```

- [ ] **Step 1: Падающие тесты `tests/test_agent_lint.py`**

```python
from bot.agent.lint import check_draft

HIST = ("Клиент: Опять касса не печатает, смену закрыть не могу\n"
        "Сотрудник: Перезагрузите кассу, пожалуйста")


def _lint(client, memo="—", **kw):
    base = dict(history=HIST, sources_text="", facts="", first_staff_reply=False,
                grounds=["KB#12", "пара#5"], source_ids=[])
    base.update(kw)
    return check_draft(client, memo, **base)


def test_password_in_memo_is_masked_but_word_in_client_is_kept():
    r = _lint("Откройте RuDesktop и пришлите ID и пароль — я подключусь.",
              memo="RuDesktop • ID 10 456 171, пароль tudiuk • телефон в тикете")
    assert "tudiuk" not in r.memo and "•••" in r.memo
    assert "пароль" in r.client
    assert "password" in r.fixed


def test_password_colon_format_is_masked():
    r = _lint("Проверьте кабель.", memo="AnyDesk 123 • пароль: abc123")
    assert "abc123" not in r.memo


def test_greeting_stripped_unless_first_reply():
    assert _lint("Добрый день! Перезагрузите роутер.").client == "Перезагрузите роутер."
    assert _lint("Добрый день! Перезагрузите роутер.", first_staff_reply=True).client.startswith("Добрый день")


def test_closer_stripped():
    r = _lint("Перезагрузите роутер. Всегда рад помочь!")
    assert r.client == "Перезагрузите роутер."
    assert "closer" in r.fixed


def test_invented_engineer_callback_is_hard():
    r = _lint("Инженер свяжется с вами по номеру для диагностики.")
    assert r.hard and "обещание" in r.hard[0]
    assert r.warning_line().startswith("⚠️ Проверь:")


def test_engineer_promise_allowed_when_history_has_it():
    hist = HIST + "\nСотрудник: Инженер банка приедет к вам завтра"
    r = _lint("Когда инженер приедет, напишите нам.", history=hist)
    assert not any("обещание" in h for h in r.hard)


def test_invented_deadline_is_hard():
    r = _lint("Разработчик проверит логи в понедельник.")
    assert any("срок" in h for h in r.hard)


def test_unknown_phone_is_hard_known_phone_is_fine():
    assert any("цифры" in h for h in _lint("Позвоните на 8 800 555 35 35.").hard)
    hist = HIST + "\nКлиент: мой номер 8 (987) 904-60-85"
    assert not _lint("Звоню на 89879046085.", history=hist).hard


def test_whitelisted_link_is_fine_unknown_link_is_hard():
    assert not _lint("Скачайте AnyDesk: https://anydesk.com/ru и пришлите номер рабочего места.").hard
    assert _lint("Скачайте драйвер: https://example.com/driver.zip").hard


def test_link_from_sources_is_fine():
    r = _lint("Инструкция: https://posiflora.teamly.ru/x", sources_text="Статья: https://posiflora.teamly.ru/x")
    assert not r.hard


def test_past_tense_self_action_is_hard():
    assert any("прошедшее" in h for h in _lint("Я подключился и настроил принтер.").hard)
    assert not _lint("Если вы уже перезагружали кассу, пришлите фото экрана.").hard


def test_unknown_source_id_is_hard():
    assert any("источник" in h for h in _lint("Смените порт.", source_ids=["KB#99"]).hard)
    assert not _lint("Смените порт.", source_ids=["KB#12"]).hard
    assert not _lint("Смените порт.", source_ids=["[KB#12]"]).hard


def test_conveyor_phrases_and_two_questions_are_soft_only():
    r = _lint("Спасибо за обращение. Какая модель? Какой кабель?")
    assert not r.hard
    assert "conveyor" in r.soft and "questions" in r.soft


def test_clean_draft_passes():
    r = _lint("Понимаю, смену надо закрыть сейчас. Переключите кабель кассы в другой "
              "USB-разъём и перезагрузите кассу. Подскажите, получилось?")
    assert not r.hard and not r.fixed and r.warning_line() == ""
```

- [ ] **Step 2: Запустить — упадёт**

Run: `python -m pytest tests/test_agent_lint.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.agent.lint'`.

- [ ] **Step 3: Реализация `bot/agent/lint.py`**

```python
"""Проверки черновика кодом, без вызовов модели (spec 2026-09-27 §5).

Замена LLM self-check. Тот вешал ⚠️ на каждый второй черновик, пометки перестали
читать. Правило здесь: пометка в Памятке только там, где почти наверняка
ошибка (hard). Мелочи идут в soft — только в базу, для статистики.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .operator_text import CLOSER_RE
from .voice import ALLOWED_URLS

_PASSWORD_RE = re.compile(
    r"(парол\w*|password|pwd)(\s*[:\-–—]?\s*)(?!(?:и|от|для)\b)([A-Za-z0-9!@#$%^&*._\-]{4,})",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|приветствую)[!.,]*\s*", re.I
)
_PROMISE_RE = re.compile(
    r"\b(инженер|специалист|разработчик|мастер|техник)\w*"
    r"(?:\W+\w+){0,3}?\W+(?:свяж|позвон|перезвон|подключ|приед|провер|ответ)\w*",
    re.I,
)
_DEADLINE_RE = re.compile(
    r"\b(?:завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|"
    r"воскресенье)|с\s+\d{1,2}(?::\d{2})?\s*(?:утра|час\w*)|в\s+течени[ея]\s+\d+)",
    re.I,
)
_PAST_SELF_RE = re.compile(
    r"\b(?:подключил(?:ся|ась)?|настроил(?:а)?|проверил(?:а)?|обновил(?:а)?|"
    r"исправил(?:а)?|перезагрузил(?:а)?)\b",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s)»\"']+")
_NUMBER_RE = re.compile(r"\+?\d[\d\s()\-]{5,}\d|\b\d+\.\d+(?:\.\d+)*\b|\b\d{4,}\b")
_CONVEYOR_RE = re.compile(
    r"спасибо за обращение|приносим\s+(?:свои\s+)?извинения|в кратчайшие сроки|"
    r"на (?:текущий|данный) момент|данный вопрос|уважаем\w+\s+(?:клиент|пользовател)|"
    r"информируем вас|должна быть решена|надеюсь,? это поможет|"
    r"если (?:у вас )?(?:возникнут|появятся|есть) вопросы|для дальнейшей диагностики|"
    r"необходимо выполнить следующие",
    re.I,
)
_MAX_WORDS = 60


@dataclass
class LintResult:
    client: str
    memo: str
    fixed: list[str] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    def warning_line(self) -> str:
        return f"⚠️ Проверь: {self.hard[0]}" if self.hard else ""

    def as_dict(self) -> dict:
        return {"fixed": self.fixed, "hard": self.hard, "soft": self.soft}


def _staff_text(history: str) -> str:
    return "\n".join(
        ln for ln in (history or "").splitlines()
        if ln.startswith(("Сотрудник:", "Коллега:"))
    ).lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _norm_url(u: str) -> str:
    return u.lower().rstrip(".,;:!?/")


def _unknown_anchor(client: str, haystack: str) -> str | None:
    hay_urls = {_norm_url(u) for u in _URL_RE.findall(haystack)}
    hay_urls |= {_norm_url(u) for u in ALLOWED_URLS}
    for url in _URL_RE.findall(client):
        if _norm_url(url) not in hay_urls:
            return url
    without_urls = _URL_RE.sub(" ", client)
    hay_numbers = [_digits(n) for n in _NUMBER_RE.findall(_URL_RE.sub(" ", haystack))]
    for raw in _NUMBER_RE.findall(without_urls):
        d = _digits(raw)
        if d and not any(d in h for h in hay_numbers if h):
            return raw.strip()
    return None


def check_draft(
    client: str,
    memo: str,
    *,
    history: str,
    sources_text: str,
    facts: str = "",
    first_staff_reply: bool = False,
    grounds: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> LintResult:
    res = LintResult(client=(client or "").strip(), memo=(memo or "").strip())

    # fix: пароль в Памятке
    masked = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", res.memo)
    if masked != res.memo:
        res.memo = masked
        res.fixed.append("password")

    # fix: приветствие не к месту и дежурная концовка
    if not first_staff_reply:
        stripped = _GREETING_RE.sub("", res.client)
        if stripped != res.client:
            res.client = stripped
            res.fixed.append("greeting")
    closed = re.sub(r"\s+", " ", CLOSER_RE.sub("", res.client)).strip()
    if closed != res.client:
        res.client = closed
        res.fixed.append("closer")

    text = res.client
    low = text.lower()
    staff = _staff_text(history)

    m = _PROMISE_RE.search(text)
    if m and m.group(1).lower()[:6] not in staff:
        res.hard.append(f"обещание за других («{m.group(0)}») — в истории его нет")

    d = _DEADLINE_RE.search(text)
    if d and d.group(0).lower() not in (history or "").lower():
        res.hard.append(f"срок «{d.group(0)}» — в истории его нет")

    anchor = _unknown_anchor(text, "\n".join([history or "", sources_text or "", facts or ""]))
    if anchor:
        res.hard.append(f"цифры или ссылка «{anchor}» не из переписки и не из источников")

    p = _PAST_SELF_RE.search(text)
    if p:
        res.hard.append(f"прошедшее время о несделанном («{p.group(0)}»)")

    # модель иногда пишет метку в скобках, как в промпте: «[KB#12]»
    known = set(grounds or [])
    unknown = [s for s in (x.strip().strip("[]") for x in (source_ids or [])) if s not in known]
    if unknown:
        res.hard.append(f"источник {unknown[0]} не находили")

    if _CONVEYOR_RE.search(low):
        res.soft.append("conveyor")
    if text.count("?") > 1:
        res.soft.append("questions")
    if len(text.split()) > _MAX_WORDS:
        res.soft.append("length")
    return res
```

- [ ] **Step 4: Запустить — должен пройти**

Run: `python -m pytest tests/test_agent_lint.py -q`
Expected: 14 passed.

Если `test_closer_stripped` падает: `CLOSER_RE` ловит «Всегда рад помочь!». Не
расширять его без теста — он общий со сверкой.

- [ ] **Step 5: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/agent/lint.py tests/test_agent_lint.py
git commit -m "feat(agent): code checks catch invented promises, stray numbers and passwords in drafts"
```

---

### Task 4: Генерация и разбор ответа v2

**Files:**
- Modify: `bot/agent/generate.py`, `bot/agent/actions.py:113-147` (`parse_agent_draft`), `bot/ai_summary.py:423-428` (`prompt_version_tag`)
- Test: `tests/test_agent_generate.py` (добавить), `tests/test_agent_actions.py` (добавить)

**Interfaces:**
- Consumes: `voice.build_prompt(context, ticket_title)` (Task 1).
- Produces:
  - `parse_agent_draft(raw) -> dict | None` возвращает также
    `"analysis": str` и `"source_ids": list[str]` (по умолчанию `""` и `[]`);
  - `generate_agent_draft(context, ticket_title, *, _call_fn=None, _prompt_fn=None,
    _format_fn=None, _sleep_fn=None) -> dict | None`. При v2: промпт из
    `voice.build_prompt`, `max_tokens=600`, один повтор через `_V2_RETRY_S=20`
    секунд при пустом ответе;
  - `prompt_version_tag()` возвращает `"voice-v2"`, когда
    `config.agent_voice_v2_enabled`.

- [ ] **Step 1: Падающие тесты**

В `tests/test_agent_actions.py` добавить:

```python
def test_parse_draft_reads_analysis_and_source_ids():
    from bot.agent.actions import parse_agent_draft
    raw = ('{"analysis": "порт занят", "action": "ANSWER", "suit": "s", "client": "c", '
           '"memo": "m", "source_ids": ["KB#1", 5, "пара#2"], "confidence": 80}')
    d = parse_agent_draft(raw)
    assert d["analysis"] == "порт занят"
    assert d["source_ids"] == ["KB#1", "пара#2"]        # не-строки отброшены


def test_parse_draft_old_format_still_works():
    from bot.agent.actions import parse_agent_draft
    d = parse_agent_draft('{"action": "ASK", "suit": "", "client": "Какая модель?", "memo": "—"}')
    assert d["analysis"] == "" and d["source_ids"] == []


def test_prompt_version_tag_voice_v2(monkeypatch):
    import bot.ai_summary as ai
    from bot.config import config
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    assert ai.prompt_version_tag() == "voice-v2"
```

В `tests/test_agent_generate.py` добавить:

```python
async def test_v2_uses_voice_prompt_small_budget_and_retries_once(monkeypatch):
    from bot.agent.generate import generate_agent_draft
    from bot.config import config
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    calls, sleeps = [], []

    async def fake_call(history, *, system, model, max_tokens, temperature, reasoning_effort):
        calls.append({"system": system, "max_tokens": max_tokens})
        if len(calls) == 1:
            return None                                    # 429 у провайдера
        return ('{"analysis": "a", "action": "ANSWER", "suit": "s", '
                '"client": "Перезагрузите роутер.", "memo": "—", "source_ids": []}')

    async def fake_sleep(s):
        sleeps.append(s)

    ctx = {"history": "Клиент: не печатает", "evidence": [], "demos": [],
           "stress": False, "first_staff_reply": False}
    draft = await generate_agent_draft(ctx, "Т", _call_fn=fake_call, _sleep_fn=fake_sleep)
    assert draft["client"] == "Перезагрузите роутер."
    assert len(calls) == 2 and sleeps == [20]
    assert calls[0]["max_tokens"] == 600
    assert "ГОЛОС ОТВЕТА КЛИЕНТУ" in calls[0]["system"]
    assert "≤20 слов" not in calls[0]["system"]
```

- [ ] **Step 2: Запустить — упадут**

Run: `python -m pytest tests/test_agent_actions.py tests/test_agent_generate.py -q`
Expected: FAIL (`KeyError: 'analysis'`, `unexpected keyword argument '_sleep_fn'`).

- [ ] **Step 3: `parse_agent_draft` в `bot/agent/actions.py`**

Перед `return {` в `parse_agent_draft` добавить:

```python
    raw_ids = obj.get("source_ids")
    source_ids = [s for s in raw_ids if isinstance(s, str)] if isinstance(raw_ids, list) else []
```

и в возвращаемый словарь — два ключа:

```python
        "analysis": str(obj.get("analysis", "")).strip(),
        "source_ids": source_ids,
```

- [ ] **Step 4: `prompt_version_tag` в `bot/ai_summary.py`**

```python
def prompt_version_tag() -> str:
    """Метка версии промпта для трассировки ai_suggestions.
    Согласована между run_agent и register_feedback_pending (общий idempotency_key)."""
    if config.agent_voice_v2_enabled:
        return "voice-v2"
    if _active_prompt_loaded and _active_format_instructions is not None:
        return "db-active"
    return "legacy"
```

- [ ] **Step 5: Ветка v2 в `generate_agent_draft` (`bot/agent/generate.py`)**

Добавить константу модуля `_V2_RETRY_S = 20` и параметр `_sleep_fn=None` в
сигнатуру. Блок ниже вставить сразу после существующей строки
`from ..config import config` (она идёт после ленивых импортов `_call_fn`,
`_prompt_fn`, `_format_fn`) и до строки `rag_examples = [...]`. Старый код ниже
не меняется.

```python
    if config.agent_voice_v2_enabled:
        from .voice import build_prompt
        if _sleep_fn is None:
            import asyncio
            _sleep_fn = asyncio.sleep
        system = build_prompt(context, ticket_title)
        raw = None
        for attempt in range(2):
            raw = await _call_fn(
                context["history"], system=system, model=config.agent_draft_model,
                max_tokens=600, temperature=0.3,
                reasoning_effort=config.groq_reasoning_effort,
            )
            if (raw or "").strip() or attempt == 1:
                break
            # ponytail: фиксированная пауза вместо x-ratelimit-reset-tokens —
            # call_groq_text не отдаёт заголовки; хватает, пока TPM-окно 60 с.
            await _sleep_fn(_V2_RETRY_S)
        draft = parse_agent_draft(raw or "")
        if draft is None:
            logger.warning("draft v2 parse failed; raw head: %s", (raw or "")[:200])
        return draft
```

- [ ] **Step 6: Прогнать тесты**

Run: `python -m pytest tests/test_agent_actions.py tests/test_agent_generate.py -q`
Expected: всё проходит (старые тесты — без флага).

- [ ] **Step 7: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/agent/generate.py bot/agent/actions.py bot/ai_summary.py tests/test_agent_generate.py tests/test_agent_actions.py
git commit -m "feat(agent): v2 draft call — voice prompt, 600-token budget, one retry after a rate limit"
```

---

### Task 5: Пайплайн v2 — проверки кодом вместо self-check, без старого фолбэка

**Files:**
- Modify: `bot/agent/pipeline.py`, `bot/topic_manager.py:210-258` (`_generate_summary_with_retry`)
- Test: `tests/test_agent_pipeline.py` (добавить), `tests/test_quiet_mode.py` (добавить)

**Interfaces:**
- Consumes: `lint.check_draft` (Task 3), поля `analysis` и `source_ids`
  (Task 4), `context["stress"]`, `context["first_staff_reply"]`,
  `context["grounds"]` (Task 2).
- Produces: `run_agent` при v2:
  - не вызывает `_selfcheck_fn`;
  - возвращает `(suit, client_после_lint, memo, confidence)`, где memo
    начинается с `⚠️ Проверь: …` при hard-нарушении;
  - пишет `self_check` как JSON `{"lint": {...}, "analysis": "..."}`.

  `_generate_summary_with_retry` при v2 и разрешённом агенте не вызывает
  `generate_ticket_summary`.

- [ ] **Step 1: Падающие тесты**

В `tests/test_agent_pipeline.py` добавить:

```python
async def test_v2_skips_selfcheck_and_applies_lint(monkeypatch):
    import json
    from bot.config import config
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    recorded = {}

    async def ctx(*a, **k):
        d = _ctx_dict()
        d.update({"demos": [], "stress": False, "first_staff_reply": False,
                  "attachments": "", "call_notes": "", "ticket_facts": ""})
        return d

    async def draft(c, title, **k):
        return {"action": "ANSWER", "suit": "s",
                "client": "Добрый день! Инженер свяжется с вами. Всегда рад помочь!",
                "memo": "RuDesktop • пароль tudiuk", "confidence": 70,
                "confidence_reason": "", "analysis": "порт", "source_ids": ["KB#12"]}

    async def selfcheck(*a, **k):
        raise AssertionError("self-check must not run on v2")

    async def record(**kw):
        recorded.update(kw)

    suit, client, memo, conf = await run_agent(
        _POSTS, _INFO, ticket_title="T", ticket_id="1",
        _context_fn=ctx, _draft_fn=draft, _selfcheck_fn=selfcheck,
        _safety_pre=_PROCEED, _safety_post=_PROCEED, _record_fn=record,
        _posts_fn=_fresh_posts_same,
    )
    assert client == "Инженер свяжется с вами."
    assert memo.startswith("⚠️ Проверь: обещание")
    assert "tudiuk" not in memo
    sc = json.loads(recorded["self_check"])
    assert sc["analysis"] == "порт" and "password" in sc["lint"]["fixed"]
```

В `tests/test_quiet_mode.py` добавить:

```python
@pytest.mark.asyncio
async def test_v2_never_falls_back_to_legacy_summary(monkeypatch):
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    monkeypatch.setattr(config, "agent_enabled", True)
    monkeypatch.setattr(config, "agent_auto_first_suggestion_enabled", True)
    monkeypatch.setattr(config, "ai_suggestion_auto_enabled", True)
    with patch("bot.topic_manager.run_agent", new_callable=AsyncMock) as agent, patch(
        "bot.topic_manager.generate_ticket_summary", new_callable=AsyncMock
    ) as legacy:
        agent.return_value = None
        result = await topic_manager._generate_summary_with_retry(
            [], SimpleNamespace(client_id=1), ticket_id="T", trigger_source="reply",
        )
    assert result is None
    legacy.assert_not_awaited()
```

- [ ] **Step 2: Запустить — упадут**

Run: `python -m pytest tests/test_agent_pipeline.py tests/test_quiet_mode.py -q`
Expected: FAIL (`AssertionError: self-check must not run on v2`, legacy awaited).

- [ ] **Step 3: Ветка v2 в `run_agent` (`bot/agent/pipeline.py`)**

Добавить переменную `self_check_json: str | None = None` рядом с
`self_status = "n/a"` перед циклом. В цикле заменить ветку
`elif action == "ANSWER":` на:

```python
        elif config.agent_voice_v2_enabled:
            from .lint import check_draft
            sources_text = "\n".join(e.get("used_excerpt", "") for e in context["evidence"])
            lint = check_draft(
                client, base_memo,
                history=context["history"],
                sources_text=sources_text,
                facts="\n".join([context.get("ticket_facts", ""),
                                 context.get("attachments", ""),
                                 context.get("call_notes", "")]),
                first_staff_reply=context.get("first_staff_reply", False),
                grounds=context.get("grounds", []),
                source_ids=draft.get("source_ids", []),
            )
            client = lint.client
            base_memo = lint.memo
            if lint.warning_line():
                base_memo = f"{lint.warning_line()}\n{base_memo}".strip()
            self_status = "lint"
            self_check_json = json.dumps(
                {"lint": lint.as_dict(), "analysis": draft.get("analysis", "")},
                ensure_ascii=False,
            )
        elif action == "ANSWER":
```

(существующая ветка self-check остаётся ниже без изменений). В начало функции,
рядом с другими ленивыми импортами, добавить `from ..config import config`.

В `_record_nonfatal` добавить параметр `self_check_json: str | None = None` и
передавать
`self_check=self_check_json or json.dumps({"status": self_status}, ensure_ascii=False)`.
В финальном вызове `_record_nonfatal(...)` из `run_agent` передать
`self_check_json=self_check_json`.

- [ ] **Step 4: Без старого фолбэка в `_generate_summary_with_retry` (`bot/topic_manager.py`)**

После блока `if agent_allowed: try: ... except ...` (перед циклом
`for attempt in range(1, attempts + 1):`) добавить:

```python
    if agent_allowed and config.agent_voice_v2_enabled:
        # v2: старый путь даёт худшие черновики (разбор 2026-09-27, §3.7) —
        # лучше без черновика, чем с «инженер свяжется с 9 утра». Кнопка 🔄 есть.
        logger.warning("agent v2 produced no draft for ticket %s (%s)", ticket_id, trigger_source)
        return None
```

- [ ] **Step 5: Прогнать тесты агента и тихого режима**

Run: `python -m pytest tests/test_agent_pipeline.py tests/test_quiet_mode.py tests/test_agent_integration.py -q`
Expected: всё проходит.

- [ ] **Step 6: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/agent/pipeline.py bot/topic_manager.py tests/test_agent_pipeline.py tests/test_quiet_mode.py
git commit -m "feat(agent): v2 drafts are checked by code, not by a second model call, and never fall back to the legacy path"
```

---

### Task 6: Доставка — одно сообщение и черновик в сообщении клиента

**Files:**
- Modify: `bot/formatter.py` (новая `format_draft_block`), `bot/handlers/ai_feedback.py` (новая `draft_kb`), `bot/topic_history.py` (`post_suggestion_messages`, новая `append_draft_to_reply`, ветка «черновика нет» в `_post_ticket_history`), `bot/topic_manager.py` (`_handle_client_reply_locked`)
- Test: `tests/test_reply_draft_delivery.py` (new)

**Interfaces:**
- Consumes: `_generate_summary_with_retry(..., trigger_source="reply")` (Task 5), `register_feedback_pending`, `_strip_prev_suggest_button`, `db.get_topic`.
- Produces:
  - `format_draft_block(client: str, memo: str, suit: str | None = None, *, separator: bool = True) -> str`;
  - `draft_kb() -> InlineKeyboardMarkup` (📤 `ai:send_post`, ✏️ `ai:edit`, 🔄 `ai:suggest`);
  - `append_draft_to_reply(bot, *, ticket_id: str, topic_id: int, message_id: int, reply_html: str, ticket_title: str) -> bool`.

- [ ] **Step 1: Падающие тесты `tests/test_reply_draft_delivery.py`**

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import topic_history
from bot.config import config
from bot.formatter import format_draft_block
from bot.hde_api import HDEPost, HDETicketInfo


def test_draft_block_escapes_html_and_hides_empty_memo():
    block = format_draft_block("Нажмите <OK> & ждите", "—")
    assert "&lt;OK&gt; &amp;" in block
    assert "<code>" in block and "📝" not in block


def test_draft_block_standalone_has_no_separator_and_suit_first():
    block = format_draft_block("c", "m", suit="Атол не печатает", separator=False)
    assert not block.startswith("\n")
    assert block.index("Атол не печатает") < block.index("<code>c</code>")


def _hde_client():
    client = MagicMock()
    client.get_ticket_info = AsyncMock(return_value=HDETicketInfo(1, "A", 2, "B"))
    client.get_ticket_posts = AsyncMock(return_value=[
        HDEPost(post_id=5, user_id=1, text="касса не печатает", date_created="10:00:00 01.01.2026")])
    client.get_ticket_comments = AsyncMock(return_value=[])
    return client


async def _run(monkeypatch, *, latest_msg_id, reply_html="👤 <b>Клиент</b>\n<blockquote>x</blockquote>",
               result=("s", "Перезагрузите кассу. Получилось?", "Атол • порт", 0)):
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    with (
        patch("bot.hde_api.HDEApiClient", return_value=_hde_client()),
        patch("bot.topic_manager._generate_summary_with_retry", new=AsyncMock(return_value=result)),
        patch("bot.topic_manager.db") as db,
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()) as reg,
    ):
        db.get_topic = AsyncMock(return_value=SimpleNamespace(suggest_button_msg_id=latest_msg_id))
        ok = await topic_history.append_draft_to_reply(
            bot, ticket_id="T", topic_id=10, message_id=77, reply_html=reply_html, ticket_title="t")
    return ok, bot, reg


async def test_draft_is_appended_to_latest_reply_with_keyboard(monkeypatch):
    ok, bot, reg = await _run(monkeypatch, latest_msg_id=77)
    assert ok
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 77 and kwargs["reply_markup"] is not None
    assert "<code>Перезагрузите кассу. Получилось?</code>" in kwargs["text"]
    reg.assert_awaited_once()


async def test_stale_draft_has_no_keyboard_and_no_pending(monkeypatch):
    """Review Focus 1: черновик на старый ответ пришёл после нового."""
    ok, bot, reg = await _run(monkeypatch, latest_msg_id=99)
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    reg.assert_not_awaited()


async def test_overflow_goes_to_separate_silent_message(monkeypatch):
    """Review Focus 2: не влезает в 4096 — отдельное тихое сообщение."""
    ok, bot, reg = await _run(monkeypatch, latest_msg_id=77, reply_html="x" * 4090)
    bot.edit_message_text.assert_not_awaited()
    assert bot.send_message.await_args.kwargs["disable_notification"] is True


async def test_no_action_appends_nothing(monkeypatch):
    ok, bot, reg = await _run(monkeypatch, latest_msg_id=77, result=("s", "", "—", 0))
    assert not ok
    bot.edit_message_text.assert_not_awaited()
    reg.assert_not_awaited()


async def test_v2_single_message_instead_of_three(monkeypatch):
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with (
        patch("bot.handlers.ai_feedback.register_feedback_pending", new=AsyncMock()),
        patch("bot.topic_manager.db") as db,
    ):
        db.update_topic = AsyncMock()
        ok = await topic_history.post_suggestion_messages(
            bot, topic_id=1, ticket_id="T", suit_line="Атол не печатает",
            client_line="Перезагрузите кассу.", memo_line="—", confidence_pct=90,
            all_posts=[], info=SimpleNamespace(client_id=1), ticket_title="t", anchor="5",
        )
    assert ok
    assert bot.send_message.await_count == 1
    assert "Атол не печатает" in bot.send_message.await_args.kwargs["text"]
```

- [ ] **Step 2: Запустить — упадут**

Run: `python -m pytest tests/test_reply_draft_delivery.py -q`
Expected: FAIL — `ImportError: cannot import name 'format_draft_block'`.

- [ ] **Step 3: `format_draft_block` в `bot/formatter.py`**

```python
def format_draft_block(
    client: str, memo: str, suit: str | None = None, *, separator: bool = True
) -> str:
    """Черновик одним блоком (spec 2026-09-27 §6).

    Текст клиенту — в <code>: в Telegram он копируется нажатием. Пустая памятка
    («—») не показывается. Всё экранируется: текст модели может содержать < и &.
    """
    lines = []
    if suit:
        lines.append(f"🧠 {_escape(suit)}")
    if client:
        lines.append(f"💡 <code>{_escape(client)}</code>")
    memo = (memo or "").strip()
    if memo and memo != "—":
        lines.append(f"📝 {_escape(memo)}")
    body = "\n".join(lines)
    return f"\n────────\n{body}" if separator else body
```

(`_escape` в модуле уже есть, им пользуется `format_client_reply`.)

- [ ] **Step 4: `draft_kb` в `bot/handlers/ai_feedback.py`**

```python
def draft_kb() -> InlineKeyboardMarkup:
    """Клавиатура черновика v2: отправить, исправить, другой вариант."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Ответить клиенту", callback_data="ai:send_post"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="ai:edit"),
        InlineKeyboardButton(text="🔄 Другой вариант", callback_data="ai:suggest"),
    ]])
```

- [ ] **Step 5: Одно сообщение в `post_suggestion_messages` (`bot/topic_history.py`)**

В начале блока `try:` функции `post_suggestion_messages`, до `if not answer_only:`,
добавить:

```python
        if _tm.config.agent_voice_v2_enabled:
            from .formatter import format_draft_block
            from .handlers.ai_feedback import draft_kb
            if not client_line and (memo_line or "").strip() in ("", "—"):
                return True                                   # NO_ACTION — молчим
            await bot.send_message(
                chat_id=_tm.config.group_chat_id,
                message_thread_id=topic_id,
                text=format_draft_block(
                    client_line, memo_line,
                    suit=suit_line if trigger_source == "first" else None,
                    separator=False,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True,
                reply_markup=draft_kb() if client_line else None,
            )
        else:
```

и сдвинуть существующие три `send_message` внутрь этого `else:` (отступ +4).
Регистрация `register_feedback_pending` и `update_topic` ниже остаются общими.

- [ ] **Step 6: `append_draft_to_reply` в `bot/topic_history.py`**

```python
_TG_LIMIT = 4096


async def append_draft_to_reply(
    bot: Bot,
    *,
    ticket_id: str,
    topic_id: int,
    message_id: int,
    reply_html: str,
    ticket_title: str,
) -> bool:
    """Черновик на ответ клиента: дописать в то же сообщение (spec §6).

    Кнопки и pending — только если это сообщение всё ещё последнее: иначе
    черновик на старый ответ мог бы уйти клиенту кнопкой 📤.
    """
    from . import topic_manager as _tm
    from .ai_summary import _build_history_text
    from .formatter import format_draft_block
    from .handlers.ai_feedback import draft_kb, register_feedback_pending
    from .hde_api import HDEApiClient, HDEApiError

    try:
        client = HDEApiClient()
        info = await client.get_ticket_info(ticket_id)
        posts = await client.get_ticket_posts(ticket_id)
        try:
            comments = await client.get_ticket_comments(ticket_id)
        except HDEApiError:
            comments = []
    except Exception as exc:
        logger.warning("reply draft: HDE fetch failed for ticket %s: %s", ticket_id, exc)
        return False

    all_posts = sorted(posts + comments, key=lambda p: p.date_created)
    anchor = str(max((p.post_id for p in all_posts), default="")) or None
    result = await _tm._generate_summary_with_retry(
        all_posts, info, ticket_title=ticket_title, ticket_id=ticket_id,
        company_id="", topic_id=topic_id, trigger_source="reply",
    )
    if result is None:
        return False
    suit_line, client_line, memo_line, _conf = result
    if not client_line and (memo_line or "").strip() in ("", "—"):
        return False                                           # NO_ACTION

    latest = await _tm.db.get_topic(ticket_id)
    is_latest = latest is not None and latest.suggest_button_msg_id == message_id
    markup = draft_kb() if (is_latest and client_line) else None
    block = format_draft_block(client_line, memo_line)
    try:
        if len(reply_html) + len(block) > _TG_LIMIT:
            await bot.send_message(
                chat_id=_tm.config.group_chat_id, message_thread_id=topic_id,
                text=block.lstrip("\n─"), parse_mode="HTML",
                disable_web_page_preview=True, disable_notification=True,
                reply_markup=markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=_tm.config.group_chat_id, message_id=message_id,
                text=reply_html + block, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup,
            )
    except TelegramAPIError as exc:
        logger.warning("reply draft: delivery failed for topic %d: %s", topic_id, exc)
        return False

    if markup is not None:
        await register_feedback_pending(
            topic_id=topic_id, ticket_id=ticket_id,
            history=_build_history_text(all_posts, info), title=ticket_title,
            answer_text=client_line,
            ai_full_text=f"Суть: {suit_line}\nКлиенту: {client_line}\nПамятка: {memo_line or '—'}",
            trigger_source="reply", context_until_post_id=anchor,
        )
    return True
```

- [ ] **Step 7: Запуск из обработчика ответа клиента (`bot/topic_manager.py`)**

В `_handle_client_reply_locked` перед первым `try:` отправки сообщения добавить
`sent = None`. После блока `try/except` отправки (до
`await _send_client_attachments(...)`) добавить:

```python
    if (
        sent is not None
        and config.agent_voice_v2_enabled
        and config.agent_reply_drafts_enabled
    ):
        from .topic_history import append_draft_to_reply
        asyncio.create_task(append_draft_to_reply(
            bot, ticket_id=record.ticket_id, topic_id=record.topic_id,
            message_id=sent.message_id, reply_html=reply_text,
            ticket_title=record.ticket_name or "",
        ))
```

- [ ] **Step 8: Кнопка на новом тикете, если черновика нет (`bot/topic_history.py`, `_post_ticket_history`)**

В ветке `if result is None:` после `logger.info(...)` добавить:

```python
        if _tm.config.agent_voice_v2_enabled:
            from .handlers.ai_feedback import suggest_button_kb
            try:
                await bot.send_message(
                    chat_id=_tm.config.group_chat_id, message_thread_id=topic_id,
                    text="💡 Черновик по первому сообщению — по кнопке",
                    disable_notification=True, reply_markup=suggest_button_kb(),
                )
            except TelegramAPIError as exc:
                logger.warning("first-message suggest button failed for topic %d: %s", topic_id, exc)
```

- [ ] **Step 9: Прогнать тесты доставки и соседей**

Run: `python -m pytest tests/test_reply_draft_delivery.py tests/test_suggest_button.py tests/test_quiet_mode.py tests/test_topic_manager.py tests/test_ai_feedback_flow.py -q`
Expected: всё проходит.

Если `test_quiet_mode.py::test_history_dump_off_but_autofill_still_runs` ломается
из-за нового сообщения с кнопкой: флаг v2 там не выставлен, сообщение не
отправляется. Если всё же ломается — проверить, что условие стоит именно на
`_tm.config.agent_voice_v2_enabled`.

- [ ] **Step 10: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/formatter.py bot/handlers/ai_feedback.py bot/topic_history.py bot/topic_manager.py tests/test_reply_draft_delivery.py
git commit -m "feat(agent): one draft message, written into the client's own message in the topic"
```

---

### Task 7: Тихий замер — судья сверки по регламенту, пополнение базы отдельно

**Files:**
- Modify: `bot/agent/reconcile.py` (`_CATEGORIES`, `_CATEGORY_LABEL`, `_build_judge_prompt`, `judge_divergence`, `reconcile_recent` stats), `bot/scheduler.py:427-464` (`_maybe_reconcile_answers`), `bot/optimizer/judge.py:98-103` (`_load_style_guide`)
- Test: `tests/test_reconcile.py` (обновить один тест, добавить новые), `tests/test_scheduler_hardening.py` или `tests/test_quiet_mode.py` (добавить)

**Interfaces:**
- Consumes: `config.reconcile_kb_distill_enabled` (Task 1).
- Produces:
  - категория `bot_better` → метка `accepted`;
  - `judge_divergence` добавляет к reason `reg=<пункт:yes|no|na,...>`;
  - stats `reconcile_recent` содержит ключ `bot_better`.

- [ ] **Step 1: Падающие тесты**

В `tests/test_reconcile.py`:

1. В тесте со словарём stats (около строки 323) добавить ключ `"bot_better": 0`
   в ожидаемый словарь.
2. Добавить:

```python
def test_judge_prompt_has_regulation_checklist_and_no_politeness_waiver():
    from bot.agent.reconcile import _build_judge_prompt
    system, _ = _build_judge_prompt("черновик", "ответ", "вопрос")
    assert "bot_better" in system
    assert "вежливость значения не имеют" not in system
    assert '"regulation"' in system
    assert "выдумал обещание" in system


async def test_bot_better_is_accepted_and_regulation_goes_to_reason():
    from bot.agent.reconcile import judge_divergence

    async def call(system, user, *, model):
        import json
        return json.dumps({"category": "bot_better", "missing": "", "reason": "шаг точнее",
                           "regulation": {"ack": "na", "one_step": "yes", "check_back": "no"}})

    verdict = await judge_divergence("ч", "о", client_text="q", _call_fn=call)
    assert verdict[0] == "bot_better"
    assert "reg=ack:na,one_step:yes,check_back:no" in verdict[1]
```

В `tests/test_quiet_mode.py` добавить:

```python
@pytest.mark.asyncio
async def test_reconcile_measures_but_does_not_distill_by_default(monkeypatch):
    monkeypatch.setattr(config, "nightly_reconcile_enabled", True)
    monkeypatch.setattr(config, "agent_dialogue_mining_enabled", True)
    monkeypatch.setattr(config, "reconcile_kb_distill_enabled", False)
    monkeypatch.setattr(scheduler, "_last_reconcile_date", None)
    monkeypatch.setattr(scheduler, "_now_msk", lambda: datetime(2026, 9, 28, 2, 5,
                        tzinfo=zoneinfo.ZoneInfo("Europe/Moscow")))
    with patch("bot.agent.reconcile.reconcile_recent", new=AsyncMock(return_value={})) as rec, \
         patch("bot.agent.kb_distill.process_pending_candidates", new=AsyncMock()) as distill, \
         patch.object(scheduler.db, "archive_unused_auto_rules", new=AsyncMock()) as archive:
        await scheduler._maybe_reconcile_answers(MagicMock())
    rec.assert_awaited_once()
    distill.assert_not_awaited()
    archive.assert_not_awaited()
```

- [ ] **Step 2: Запустить — упадут**

Run: `python -m pytest tests/test_reconcile.py tests/test_quiet_mode.py -q`
Expected: FAIL.

- [ ] **Step 3: Судья сверки (`bot/agent/reconcile.py`)**

```python
_CATEGORIES = (
    "same_action", "bot_better", "bot_escalated", "bot_wrong_fact", "context_gap",
    "not_comparable",
)

_CATEGORY_LABEL = {
    "same_action": "accepted",
    "bot_better": "accepted",
    "bot_escalated": "corrected",
    "bot_wrong_fact": "corrected",
}

_REG_KEYS = ("ack", "one_step", "why", "risk", "check_back", "no_conveyor", "no_invented_promise")
```

В `_build_judge_prompt`:

1. После абзаца про `same_action` добавить:

```python
        "bot_better — по сути черновик точнее или безопаснее ответа оператора (оператор "
        "тоже ошибается; ответы написаны до нового регламента общения).\n"
```

2. Строку `"основное действие совпадает — это same_action. Разный порядок слов, "
   "объём и вежливость значения не имеют. Ставь bot_wrong_fact только когда "`
   заменить на:

```python
        "основное действие совпадает — это same_action. Разный порядок слов и объём "
        "на категорию не влияют: форму оценивает отдельный чек-лист ниже. Ставь "
        "bot_wrong_fact только когда "
```

3. Перед строкой `'Верни СТРОГО JSON: ...'` добавить:

```python
        "Если черновик выдумал обещание (инженер свяжется, звонок, срок) или отправил "
        "клиента ждать специалиста, а оператор дал шаг — это bot_escalated, даже если "
        "контекста не хватало.\n"
        "Отдельно оцени ФОРМУ черновика по регламенту (не сравнивая с оператором), "
        "каждый пункт yes|no|na: ack — признал конкретное неудобство, если клиент "
        "раздражён или спешит; one_step — один шаг или один вопрос; why — объяснил зачем, "
        "если просит данные; risk — предупредил о риске до шага; check_back — попросил "
        "проверить результат после инструкции; no_conveyor — нет дежурных фраз; "
        "no_invented_promise — нет выдуманных обещаний.\n"
```

4. В JSON-схеме ответа заменить `"category":"same_action|context_gap|bot_escalated|'`
   на `"category":"same_action|bot_better|context_gap|bot_escalated|'` и после
   `"reason":"кратко по-русски"` добавить
   `,"regulation":{"ack":"yes|no|na","one_step":"...","why":"...","risk":"...","check_back":"...","no_conveyor":"...","no_invented_promise":"..."}`.

В `judge_divergence` после вычисления `reason` (и строки для `context_gap`) добавить:

```python
    reg = obj.get("regulation")
    if isinstance(reg, dict):
        compact = ",".join(
            f"{k}:{reg[k]}" for k in _REG_KEYS if str(reg.get(k, "")) in ("yes", "no", "na")
        )
        if compact:
            reason = f"{reason} reg={compact}".strip()
```

В `reconcile_recent` в словарь `stats` добавить `"bot_better": 0`.

- [ ] **Step 4: Разделение флагов (`bot/scheduler.py`, `_maybe_reconcile_answers`)**

Блоки «Разбор очереди кандидатов» (`process_pending_candidates`) и
«archive_unused_auto_rules» обернуть условием:

```python
    if not config.reconcile_kb_distill_enabled:
        return
```

Условие ставится сразу после `try/except` с `reconcile_recent`. Обновить
docstring: замер — `nightly_reconcile_enabled`, пополнение базы —
`reconcile_kb_distill_enabled`.

- [ ] **Step 5: Судья оптимизатора читает голос (`bot/optimizer/judge.py`)**

```python
def _load_style_guide() -> str:
    """Голос по регламенту (spec 2026-09-27 §4.2); старый стайлгайд — запасной."""
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    for name in ("voice_ru.md", "style_guide_ru.md"):
        try:
            return (prompts / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return ""
```

- [ ] **Step 6: Прогнать тесты**

Run: `python -m pytest tests/test_reconcile.py tests/test_reconcile_context.py tests/test_quiet_mode.py tests/test_judge.py tests/test_kb_distill.py -q`
Expected: всё проходит.

- [ ] **Step 7: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add bot/agent/reconcile.py bot/scheduler.py bot/optimizer/judge.py tests/test_reconcile.py tests/test_quiet_mode.py
git commit -m "feat(reconcile): the nightly judge checks drafts against the regulation, and measuring no longer feeds the knowledge base"
```

---

### Task 8: Офлайн-проверка «старый путь против нового»

**Files:**
- Create: `scripts/eval_voice.py`, `tests/test_eval_voice.py`
- Create (данные, на сервере): `data/golden/voice_v2_set.json`, `data/golden/regulation_gold.json`, `artifacts/voice_eval.json`, `artifacts/voice_ab_pairs.json`, `artifacts/voice_ab_key.json`

**Interfaces:**
- Consumes: `build_agent_context`, `generate_agent_draft`, `lint.check_draft`,
  `config.agent_voice_v2_enabled`, `HDEApiClient`, прод-БД (только чтение).
- Produces (чистые функции в `scripts/eval_voice.py`, импортируемые тестом):
  - `pattern_flags(text: str) -> dict[str, bool]` — ключи `remote`, `wait`,
    `check_back`, `multi_question`;
  - `summarize(rows: list[dict]) -> dict` — доли по старому и новому пути;
  - `make_ab_pairs(rows, seed=42) -> tuple[list[dict], dict]` — пары и ключ.

- [ ] **Step 1: Падающий тест `tests/test_eval_voice.py`**

```python
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "eval_voice", Path(__file__).resolve().parents[1] / "scripts" / "eval_voice.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_pattern_flags():
    f = ev.pattern_flags("Скачайте AnyDesk и ждите звонка инженера. Получилось? Какая модель?")
    assert f == {"remote": True, "wait": True, "check_back": True, "multi_question": True}
    assert not any(ev.pattern_flags("Перезагрузите роутер.").values())


def test_summarize_rates():
    rows = [{"old": {"client": "Скачайте AnyDesk"}, "new": {"client": "Перезагрузите роутер. Получилось?"}},
            {"old": {"client": "Ждите звонка инженера"}, "new": {"client": "Смените порт."}}]
    s = ev.summarize(rows)
    assert s["old"]["remote"] == 0.5 and s["new"]["remote"] == 0.0
    assert s["old"]["wait"] == 0.5 and s["new"]["check_back"] == 0.5


def test_ab_pairs_are_blind_and_reproducible():
    rows = [{"case_id": i, "old": {"client": f"o{i}"}, "new": {"client": f"n{i}"}} for i in range(6)]
    pairs, key = ev.make_ab_pairs(rows, seed=1)
    assert all(set(p) == {"case_id", "A", "B"} for p in pairs)
    assert {key[str(p["case_id"])] for p in pairs} <= {"A=old", "A=new"}
    assert ev.make_ab_pairs(rows, seed=1) == (pairs, key)
```

- [ ] **Step 2: Запустить — упадёт**

Run: `python -m pytest tests/test_eval_voice.py -q`
Expected: FAIL — файла `scripts/eval_voice.py` нет.

- [ ] **Step 3: Реализация `scripts/eval_voice.py`**

```python
"""Офлайн-проверка агента v2 против старого пути (spec 2026-09-27 §7).

Запуск на сервере (там прод-БД, модель эмбеддингов и ключ Groq):
  python scripts/eval_voice.py build --n 50     # набор из прод-БД + посты из HDE
  python scripts/eval_voice.py run              # ~100 запросов к Groq, ночью
  python scripts/eval_voice.py report           # счётчики + слепые пары для A/B

Генерация идёт через настоящий код агента (build_agent_context +
generate_agent_draft), а не через упрощённый вызов: иначе проверка мерит не тот
пайплайн (урок разбора 2026-09-27, §3.9).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SET_PATH = ROOT / "data" / "golden" / "voice_v2_set.json"
RUN_PATH = ROOT / "artifacts" / "voice_eval.json"
PAIRS_PATH = ROOT / "artifacts" / "voice_ab_pairs.json"
KEY_PATH = ROOT / "artifacts" / "voice_ab_key.json"

_REMOTE = re.compile(r"anydesk|rudesktop|анидеск|рудесктоп|удал[её]нн", re.I)
_WAIT = re.compile(r"инженер|специалист|свяжет|ожидайте|ждите|звонк|передад", re.I)
_CHECK = re.compile(r"получилось|сработало|заработал|проверьте,? (?:пожалуйста,? )?сейчас", re.I)
_PACE_S = 20      # 8000 TPM на qwen3.8: один запрос ~6k токенов


def pattern_flags(text: str) -> dict[str, bool]:
    text = text or ""
    return {
        "remote": bool(_REMOTE.search(text)),
        "wait": bool(_WAIT.search(text)),
        "check_back": bool(_CHECK.search(text)),
        "multi_question": text.count("?") > 1,
    }


def summarize(rows: list[dict]) -> dict:
    out = {}
    for side in ("old", "new"):
        texts = [(r.get(side) or {}).get("client", "") for r in rows]
        n = max(len(texts), 1)
        flags = [pattern_flags(t) for t in texts]
        out[side] = {k: round(sum(f[k] for f in flags) / n, 3) for k in flags[0]} if flags else {}
        hard = [len(((r.get(side) or {}).get("lint") or {}).get("hard", [])) for r in rows]
        out[side]["hard_lint_share"] = round(sum(1 for h in hard if h) / n, 3)
    return out


def make_ab_pairs(rows: list[dict], seed: int = 42) -> tuple[list[dict], dict]:
    rnd = random.Random(seed)
    pairs, key = [], {}
    for r in rows:
        old, new = (r["old"] or {}).get("client", ""), (r["new"] or {}).get("client", "")
        if rnd.random() < 0.5:
            pairs.append({"case_id": r["case_id"], "A": old, "B": new})
            key[str(r["case_id"])] = "A=old"
        else:
            pairs.append({"case_id": r["case_id"], "A": new, "B": old})
            key[str(r["case_id"])] = "A=new"
    return pairs, key


async def build(n: int, db_path: str) -> None:
    from bot.hde_api import HDEApiClient
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id, ticket_id, title, context_until_post_id, judge_reference_answer "
        "FROM ai_suggestions WHERE judge_reference_answer IS NOT NULL "
        "AND judge_reference_answer != '' ORDER BY id DESC"
    ).fetchall()
    seen, cases = set(), []
    client = HDEApiClient()
    for sid, ticket_id, title, anchor, reference in rows:
        if ticket_id in seen or len(cases) >= n:
            continue
        seen.add(ticket_id)
        info = await client.get_ticket_info(str(ticket_id))
        posts = await client.get_ticket_posts(str(ticket_id))
        comments = await client.get_ticket_comments(str(ticket_id))
        await asyncio.sleep(1.2)                  # HDE: 300 req/min на весь аккаунт
        cut = int(anchor or 0)
        kept = [p for p in posts + comments if int(p.post_id) <= cut]
        cases.append({
            "case_id": sid, "ticket_id": str(ticket_id), "title": title or "",
            "reference": reference,
            "info": {"client_id": info.client_id, "client_name": info.client_name,
                     "owner_id": info.owner_id, "owner_name": info.owner_name},
            "posts": [{"post_id": p.post_id, "user_id": p.user_id, "text": p.text,
                       "date_created": p.date_created, "is_comment": p.is_comment}
                      for p in kept],
        })
    SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SET_PATH.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {len(cases)} cases → {SET_PATH}")


async def _one(case: dict, v2: bool) -> dict:
    from bot.agent.context import build_agent_context
    from bot.agent.generate import generate_agent_draft
    from bot.agent.lint import check_draft
    from bot.config import config
    from bot.hde_api import HDEPost, HDETicketInfo

    config.agent_voice_v2_enabled = v2        # обычный изменяемый экземпляр, как в тестах
    posts = [HDEPost(**p) for p in case["posts"]]
    info = HDETicketInfo(**case["info"])
    ctx = await build_agent_context(posts, info, case["title"], ticket_id=case["ticket_id"])
    draft = await generate_agent_draft(ctx, case["title"]) or {}
    lint = check_draft(
        draft.get("client", ""), draft.get("memo", ""), history=ctx["history"],
        sources_text="\n".join(e.get("used_excerpt", "") for e in ctx["evidence"]),
        first_staff_reply=ctx.get("first_staff_reply", False),
        grounds=ctx.get("grounds", []), source_ids=draft.get("source_ids", []),
    )
    return {"action": draft.get("action"), "client": draft.get("client", ""),
            "memo": draft.get("memo", ""), "analysis": draft.get("analysis", ""),
            "lint": lint.as_dict()}


async def run() -> None:
    cases = json.loads(SET_PATH.read_text(encoding="utf-8"))["cases"]
    rows = []
    for i, case in enumerate(cases):
        row = {"case_id": case["case_id"], "ticket_id": case["ticket_id"],
               "reference": case["reference"]}
        for side, v2 in (("old", False), ("new", True)):
            if i or side == "new":
                await asyncio.sleep(_PACE_S)
            row[side] = await _one(case, v2)
        rows.append(row)
        print(f"{i + 1}/{len(cases)}", flush=True)
    RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUN_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def report() -> None:
    rows = json.loads(RUN_PATH.read_text(encoding="utf-8"))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
    pairs, key = make_ab_pairs(rows)
    PAIRS_PATH.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    KEY_PATH.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"A/B pairs → {PAIRS_PATH} (key: {KEY_PATH})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--n", type=int, default=50)
    b.add_argument("--db", default=str(ROOT / "hde_bot.db"))
    sub.add_parser("run")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "build":
        asyncio.run(build(a.n, a.db))
    elif a.cmd == "run":
        asyncio.run(run())
    else:
        report()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Запустить тест**

Run: `python -m pytest tests/test_eval_voice.py -q`
Expected: 3 passed.

- [ ] **Step 5: Весь набор и коммит**

Run: `python -m pytest tests/ -q` → зелёный.

```bash
git add scripts/eval_voice.py tests/test_eval_voice.py
git commit -m "feat(eval): offline old-vs-new draft comparison through the real agent code"
```

- [ ] **Step 6: Прогон на сервере (после деплоя с выключенными флагами, Task 10)**

На config1 из `/opt/hde-bot`:

```bash
python3 scripts/eval_voice.py build --n 50
nohup python3 scripts/eval_voice.py run > logs/eval_voice.log 2>&1 &
```

После окончания: `python3 scripts/eval_voice.py report`. Затем `scp`
`artifacts/voice_eval.json`, `artifacts/voice_ab_pairs.json` и
`artifacts/voice_ab_key.json` в локальный `artifacts/` (он в `.gitignore`).

- [ ] **Step 7: 20 эталонов по регламенту и слепое A/B**

1. Ассистент пишет `data/golden/regulation_gold.json`: для 20 кейсов из набора
   (разные действия, не меньше 3 со стрессом) формат
   `{"case_id": N, "gold_client": "..."}`. Суть берётся из `reference`, форма —
   по `voice_ru.md`. Владелец правит файл.
2. Claude-судья в сессии оценивает `voice_ab_pairs.json`, не открывая ключ:
   - суть: то же действие / бот лучше / бот хуже относительно `reference`;
   - чек-лист регламента по каждому варианту;
   - «отправил бы почти как есть?».

   На 20 эталонах — какой вариант ближе к `gold_client`. Потом ключ
   раскрывается.
3. Критерии включения — spec §1. Результат записать в
   `docs/agent-review-2026-09-27.md`, раздел «Итоги проверки v2» (добавить в
   конец).

---

### Task 9: Скилл anti-slop-ru с жанром «поддержка»

**Files:**
- Create: `.claude/skills/anti-slop-ru/` — содержимое `skill.zip` плюс правки ниже; `.claude/skills/anti-slop-ru/references/support-reply.md`, `.claude/skills/anti-slop-ru/references/eval-support.md`
- Modify (внутри скилла): `SKILL.md`, `references/russian-patterns.md`, `scripts/lint_ru.py`

**Interfaces:**
- Consumes: регламент, `bot/prompts/voice_ru.md` (Task 1), пары из `tests/test_agent_lint.py` (Task 3).
- Produces: скилл, который Claude подхватывает в этом репозитории
  (`/prompt-cycle`, подготовка эталонов).

- [ ] **Step 1: Распаковать скилл**

```bash
mkdir -p .claude/skills
python -c "import zipfile; zipfile.ZipFile('skill.zip').extractall('.claude/skills')"
ls .claude/skills/anti-slop-ru
```

Expected: `SKILL.md agents references scripts`.

- [ ] **Step 2: `references/support-reply.md`**

```markdown
# Ответ клиенту в поддержке (чат, мессенджер, тикет)

Приоритетный источник — регламент «Лояльная техническая поддержка»
(`docs/Лояльная техническая поддержка — anti-slop.md`). Где этот файл спорит с
общими правилами скилла, прав регламент.

## Части ответа (только нужные, в этом порядке)
1. Признание — только если клиент раздражён, спешит или пишет повторно. Называет
   конкретное влияние («смену надо закрыть») и сразу переходит к делу.
2. Риск — если шаг прервёт продажи или может потерять данные, до шага.
3. Один шаг или один вопрос. Просишь данные — объясни зачем.
4. Проверка результата после инструкции.

Обычно 1–2 предложения, до 3 и около 50 слов. Реальные ответы операторов — около
10 слов: «лояльно» не значит «длинно».

## Конвейер (править всегда)
Дежурное приветствие в идущем диалоге; «спасибо за обращение»; «приносим извинения
за неудобства» без действия; «в кратчайшие сроки», «на текущий момент»,
«данный вопрос находится на рассмотрении», «уважаемый клиент», «информируем вас»;
«проблема должна быть решена» вместо просьбы проверить; вопросы-обвинения
(«вы точно подключили?»); обещания за других и сроки, которых нет в переписке.

## Не считать слопом
Конкретное признание неудобства со следующим шагом («Понимаю, это мешает работать
прямо сейчас. Давайте разбираться»). «Пожалуйста» один раз. Первое лицо о следующем
действии оператора («пришлите номер — я подключусь»). Названия элементов так, как
клиент видит их на экране.
```

- [ ] **Step 3: Правка `references/russian-patterns.md` §9**

Заменить абзац раздела «## 9. Фальшивая эмпатия и чат-ботный тон» на:

```markdown
## 9. Фальшивая эмпатия и чат-ботный тон
Флагать общую эмпатию без действия: «вы не одиноки», «я понимаю, как это может быть
сложно», «конечно!», «давайте разберёмся» как пустой зачин. Не флагать признание,
которое называет конкретное влияние на человека и сразу переходит к шагу: в жанре
поддержки это требование регламента (см. `support-reply.md`).
```

- [ ] **Step 4: Soft-правило поддержки в `scripts/lint_ru.py`**

В список `SOFT` добавить:

```python
    ("support_conveyor", re.compile(r"спасибо за обращение|приносим\s+(?:свои\s+)?извинения|в\s+кратчайшие\s+сроки|на\s+(?:текущий|данный)\s+момент|данный\s+вопрос|уважаем\w+\s+(?:клиент|пользовател)|информируем\s+вас|должна\s+быть\s+решена", re.I)),
```

В `self_test()` добавить перед `print("OK")`:

```python
    c=scan("Спасибо за обращение. Информируем вас, что проблема должна быть решена.")
    assert any(x['rule']=='support_conveyor' for x in c)
    d=scan("Понимаю, смену надо закрыть сейчас. Переключите кабель в другой разъём. Получилось?")
    assert not any(x['rule']=='support_conveyor' for x in d)
```

Run: `python .claude/skills/anti-slop-ru/scripts/lint_ru.py --self-test`
Expected: `OK`.

- [ ] **Step 5: `references/eval-support.md` и `SKILL.md`**

`references/eval-support.md` — 10 пар «плохо → хорошо» на реальных тикетах:

```markdown
# Eval-набор: ответ клиенту в поддержке

### 1. #953 — стресс, смена
Плохо: «Примите запрос на подключение в AnyDesk, я подключаюсь к вашему компьютеру.»
Почему: нет признания при стрессе, удалёнка вместо быстрого шага, нет проверки.
Хорошо: «Понимаю, смену надо закрыть сейчас, давайте быстро. Переключите кабель кассы в другой USB-разъём и перезагрузите кассу кнопкой питания. Подскажите, получилось?»

### 2. #1090 — ответ на «спасибо, ждём»
Плохо: «Подождите звонка инженера по номеру +7…, он свяжется для диагностики.»
Почему: выдуманный звонок; ответ на благодарность вместо проблемы.
Хорошо: «Подключите, пожалуйста, кассу в другой USB-разъём и попробуйте закрыть смену ещё раз. Получилось?»

### 3. #1116 — выдуманные инженер и время
Плохо: «Инженер свяжется с вами по номеру … с 9 утра по МСК для диагностики.»
Почему: обещание за другого и срок, которых нет в переписке.
Хорошо: «Чтобы увидеть, что происходит с оплатой, мне нужно подключиться к компьютеру. Скачайте AnyDesk: https://anydesk.com/ru, запустите и пришлите номер рабочего места — я подключусь.»

### 4. #881 — удалёнка вместо быстрого шага
Плохо: «Запустите AnyDesk и пришлите ID для подключения, чтобы я увидел ошибку.»
Почему: есть быстрый шаг, который клиент сделает сам.
Хорошо: «Закройте программу Posiflora полностью, откройте снова и попробуйте провести заказ ещё раз. Подскажите, получилось?»

### 5. Приветствие в идущем диалоге
Плохо: «Добрый день! Спасибо, что обратились! Перезагрузите роутер.»
Почему: дежурное приветствие посреди диалога.
Хорошо: «Перезагрузите, пожалуйста, роутер и терминал, затем проведите оплату ещё раз. Получилось?»

### 6. Канцелярит
Плохо: «Информируем вас, что данный вопрос находится на рассмотрении и будет решён в кратчайшие сроки.»
Почему: за формулировкой не видно человека и следующего шага.
Хорошо: «Ошибка на стороне банка: терминал не получает ответ от их сервера. Пока можно принять оплату через «Автономный терминал» в способах оплаты.»

### 7. Анкета вместо одного вопроса
Плохо: «Какая модель кассы? Какая версия Posiflora? Как подключён принтер? Что на экране?»
Почему: четыре вопроса сразу.
Хорошо: «Подскажите, как сейчас подключён принтер — по USB или по Wi-Fi? От этого зависит, что проверять первым.»

### 8. Вопрос-обвинение
Плохо: «А вы точно правильно подключили кабель?»
Почему: перекладывает вину на клиента.
Хорошо: «Давайте посмотрим, как подключено. Пришлите, пожалуйста, фото разъёма на кассе.»

### 9. Закрытие без проверки
Плохо: «Проблема должна быть решена.»
Почему: диалог закрыт до подтверждения клиента.
Хорошо: «Проверьте, пожалуйста, сейчас. Если не заработает, продолжим.»

### 10. Прошедшее время о несделанном
Плохо: «Я подключился и настроил принтер.»
Почему: оператор ещё ничего не делал, клиент будет ждать несуществующего результата.
Хорошо: «Пришлите номер рабочего места AnyDesk — я подключусь и настрою принтер.»
```

В `SKILL.md` в список профилей раздела 2 добавить строку
`- ответ клиенту в поддержке / чат — загрузить references/support-reply.md;
регламент поддержки приоритетнее общих правил;` и в «Ресурсы» — строки для
`support-reply.md` и `eval-support.md`.

- [ ] **Step 6: Коммит**

```bash
git add .claude/skills/anti-slop-ru
git commit -m "feat(skill): anti-slop-ru learns the support-reply genre and defers to the loyal-support regulation"
```

---

### Task 10: Деплой с выключенными флагами, прогон проверки, включение

**Files:** нет изменений кода; команды на config1. Перед push — финальный прогон
всего набора.

- [ ] **Step 1: Финальная проверка и push ветки**

```bash
python -m pytest tests/ -q
git push -u origin feat/agent-voice-v2
```

Слияние в `main` — после ревью ветки (whole-branch review).

- [ ] **Step 2: Деплой (флаги v2 выключены — поведение прода не меняется)**

После слияния в `main`:

```bash
ssh config1 "cd /opt/hde-bot && git pull --ff-only && sudo -n systemctl restart hde-bot && sudo -n systemctl is-active hde-bot"
```

Expected: `active`. В `/opt/hde-bot/logs/bot.log` нет `Traceback` за первые
2 минуты.

- [ ] **Step 3: Прогон проверки** — Task 8, шаги 6–7.

- [ ] **Step 4: Включение по шагам, только после критериев spec §1**

1. В `/opt/hde-bot/.env`:
   ```
   AGENT_VOICE_V2_ENABLED=1
   AI_SUGGESTION_AUTO_ENABLED=1
   NIGHTLY_RECONCILE_ENABLED=1
   ```
   Сначала бэкап: `cp .env .env.bak-<дата>`. Затем restart и проверка
   `is-active`.
2. Через несколько рабочих дней без проблем: `AGENT_REPLY_DRAFTS_ENABLED=1`,
   restart.
3. Откат любого шага: вернуть флаг в `0` и перезапустить бота.

- [ ] **Step 5: Отчёт и память**

- `record_work` в хранилище: что включено, цифры проверки;
- обновить `docs/architecture-2026-08.md` разделом про флаги v2 и тихого режима;
- обновить заметку `prompt-optimization-state` в памяти: v15 больше не
  определяет стиль агентного пути.

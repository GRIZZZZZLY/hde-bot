# Priority/Type Ticket Field Autofill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Бот автоматически выставляет поля «Приоритет» (`priority_id`) и «Тип» (`type_id`) тикета HDE одним LLM-вызовом по матрице из 6 комбинаций, однократно, с логом поправок операторов.

**Architecture:** Новый классификатор `classify_priority_type()` в `bot/ticket_fields.py` по образцу `classify_environment()` (Groq, temperature 0, lenient-парсер номера комбинации). Вызывается из `apply_ticket_fields()`; запись в HDE — тем же PUT через расширенный `update_ticket_fields()`. Предсказания хранятся в двух новых колонках `ticket_topics`; при закрытии тикета пишется `data/priority_corrections.jsonl`.

**Tech Stack:** Python 3.12, aiohttp, aiogram, Groq API (llama-3.3-70b-versatile + fallback Llama 4 Scout), SQLite (aiosqlite), pytest + pytest-asyncio.

**Спека:** `docs/superpowers/specs/2026-06-12-priority-type-autofill-design.md`

**ID значений (выяснены 2026-06-12 через GET `/priorities/` и `/types/`):**

| Поле | Значение | ID |
|---|---|---|
| priority_id | Стандарт оборуд | 10 |
| priority_id | Ускоренный 2я\оборуд | 1 |
| priority_id | Низкий 2я\оборуд | 3 |
| type_id | Вопрос | 0 |
| type_id | Задача | 2 |
| type_id | Ошибка | 3 |

⚠️ `type_id` «Вопрос» = 0 — falsy! Везде проверять `is not None`, не truthiness.

**Прогон тестов:** `python -m pytest tests/ -x -q` из корня `d:\HDE_bot`. Текущая база: 321 тест зелёный.

---

### Task 1: Константы, промпт и парсер комбинаций

**Files:**
- Modify: `bot/ticket_fields.py` (после блока `_KEYWORD_PATTERNS`/`_keyword_match`, перед `_GROQ_URL`)
- Test: `tests/test_priority_type_autofill.py` (создать)

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_priority_type_autofill.py`:

```python
"""Tests for Приоритет/Тип autofill: combo table, prompt, parser,
classifier, HDE client extension, DB columns, apply wiring, outcome log."""
import pytest

from bot import db as _db


@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))


# --- combo table & parser ---

def test_pt_combos_cover_six_rules():
    from bot.ticket_fields import _PT_COMBOS
    assert set(_PT_COMBOS) == {"1", "2", "3", "4", "5", "6"}
    # ускоренный+ошибка, стандарт+ошибка, ускоренный+задача,
    # стандарт+задача, низкий+вопрос, низкий+задача
    assert _PT_COMBOS["1"] == ("1", "3")
    assert _PT_COMBOS["2"] == ("10", "3")
    assert _PT_COMBOS["3"] == ("1", "2")
    assert _PT_COMBOS["4"] == ("10", "2")
    assert _PT_COMBOS["5"] == ("3", "0")
    assert _PT_COMBOS["6"] == ("3", "2")


def test_parse_pt_combo_plain_number():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("1") == ("1", "3")


def test_parse_pt_combo_with_reasoning_text():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("Касса не работает, торговля стоит. Ответ: 1") == ("1", "3")


def test_parse_pt_combo_undetermined():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("НЕ ОПРЕДЕЛЕНО") is None


def test_parse_pt_combo_out_of_range_rejected():
    from bot.ticket_fields import _parse_pt_combo
    # 7 и 10 — не номера комбинаций; "10" не должен распадаться на "1"
    assert _parse_pt_combo("7") is None
    assert _parse_pt_combo("10") is None


def test_parse_pt_combo_empty():
    from bot.ticket_fields import _parse_pt_combo
    assert _parse_pt_combo("") is None


def test_pt_prompt_contains_all_combos_and_undetermined():
    from bot.ticket_fields import _build_pt_prompt
    p = _build_pt_prompt()
    for num in ("1 =", "2 =", "3 =", "4 =", "5 =", "6 ="):
        assert num in p
    assert "НЕ ОПРЕДЕЛЕНО" in p
    assert "инженера банка" in p.lower() or "инженер банка" in p.lower()
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL, `ImportError: cannot import name '_PT_COMBOS'`

- [ ] **Step 3: Реализация в `bot/ticket_fields.py`**

Вставить после функции `_keyword_match` (строка ~93), перед `_GROQ_URL`:

```python
# --- Приоритет + Тип -------------------------------------------------------
# Стандартные поля тикета (не custom_fields). ID из справочников HDE
# GET /priorities/ и /types/ (проверено 2026-06-12).
PRIORITY_USKORENNY_OBORUD = "1"   # «Ускоренный 2я\оборуд»
PRIORITY_STANDART_OBORUD = "10"   # «Стандарт оборуд»
PRIORITY_NIZKIY_OBORUD = "3"      # «Низкий 2я\оборуд»

TYPE_VOPROS = "0"    # «Вопрос» — ноль валиден, проверять `is not None`!
TYPE_ZADACHA = "2"   # «Задача»
TYPE_OSHIBKA = "3"   # «Ошибка»

# Номер комбинации -> (priority_id, type_id). Бот никогда не ставит
# «Инцидент», «1я линия основной», «Стандарт 2я», «ИИ».
_PT_COMBOS: dict[str, tuple[str, str]] = {
    "1": (PRIORITY_USKORENNY_OBORUD, TYPE_OSHIBKA),
    "2": (PRIORITY_STANDART_OBORUD, TYPE_OSHIBKA),
    "3": (PRIORITY_USKORENNY_OBORUD, TYPE_ZADACHA),
    "4": (PRIORITY_STANDART_OBORUD, TYPE_ZADACHA),
    "5": (PRIORITY_NIZKIY_OBORUD, TYPE_VOPROS),
    "6": (PRIORITY_NIZKIY_OBORUD, TYPE_ZADACHA),
}


def _build_pt_prompt() -> str:
    return (
        "Ты классифицируешь обращение в техподдержку кассового ПО/оборудования "
        "по срочности (приоритет) и типу.\n"
        "Допустимые комбинации (формат «номер = Приоритет + Тип — когда выбирать»):\n\n"
        "1 = Ускоренный + Ошибка — торговля невозможна: не работает касса или "
        "платёжный терминал, продажи остановлены\n"
        "2 = Стандартный + Ошибка — что-то не работает, но торговля продолжается "
        "(например, не печатает принтер этикеток)\n"
        "3 = Ускоренный + Задача — приход инженера банка в магазин "
        "(визит специалиста, который не будет ждать очереди)\n"
        "4 = Стандартный + Задача — подключение, настройка или перенастройка "
        "оборудования (касса, принтер, другой формат этикетки, смена IP)\n"
        "5 = Низкий + Вопрос — вопрос или консультация, либо что-то непонятное, "
        "требующее изучения и поиска решения\n"
        "6 = Низкий + Задача — несрочная работа или доработка без чёткого срока "
        "(редкий случай)\n\n"
        "Прочитай переписку и выбери ровно одну комбинацию. Если уверенно "
        "определить нельзя — ответь «НЕ ОПРЕДЕЛЕНО».\n"
        "В конце ответа укажи только номер комбинации (или «НЕ ОПРЕДЕЛЕНО»)."
    )


def _parse_pt_combo(raw: str) -> tuple[str, str] | None:
    """(priority_id, type_id) по первому валидному номеру комбинации в ответе.

    Lenient, как _parse_env_id: терпит текст вокруг, отклоняет числа вне 1–6
    («10» — это токен «10», на «1» не распадается).
    """
    for tok in re.findall(r"\d+", raw):
        if tok in _PT_COMBOS:
            return _PT_COMBOS[tok]
    return None
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: PASS (7 тестов)

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py bot/ticket_fields.py
git commit -m "feat(autofill): priority/type combo table, prompt and parser"
```

---

### Task 2: Классификатор `classify_priority_type()`

**Files:**
- Modify: `bot/ticket_fields.py` (после `classify_environment`, строка ~196)
- Test: `tests/test_priority_type_autofill.py`

- [ ] **Step 1: Падающие тесты**

Дописать в `tests/test_priority_type_autofill.py`:

```python
# --- classify_priority_type ---

@pytest.mark.asyncio
async def test_classify_pt_returns_combo(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(return_value="1")
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type(
        "Клиент: касса не включается, продавать не можем",
        ticket_title="Касса не работает",
    )
    assert result == ("1", "3")
    prompt_arg, user_content = groq.await_args.args
    assert "НЕ ОПРЕДЕЛЕНО" in prompt_arg
    assert "Тема тикета: Касса не работает" in user_content
    assert "Переписка:\nКлиент: касса не включается" in user_content


@pytest.mark.asyncio
async def test_classify_pt_falls_back_to_scout(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock(side_effect=[None, "4"])
    monkeypatch.setattr(tf, "_groq_classify", groq)
    result = await tf.classify_priority_type("Клиент: настройте принтер")
    assert result == ("10", "2")
    assert groq.await_count == 2
    assert groq.await_args_list[1].kwargs["model"] == tf._GROQ_FALLBACK_MODEL


@pytest.mark.asyncio
async def test_classify_pt_undetermined(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    monkeypatch.setattr(tf, "_groq_classify", AsyncMock(return_value="НЕ ОПРЕДЕЛЕНО"))
    assert await tf.classify_priority_type("Клиент: привет") is None


@pytest.mark.asyncio
async def test_classify_pt_empty_input_skips_llm(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    groq = AsyncMock()
    monkeypatch.setattr(tf, "_groq_classify", groq)
    assert await tf.classify_priority_type("", ticket_title="") is None
    groq.assert_not_awaited()
```

- [ ] **Step 2: Прогнать — падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL, `AttributeError: ... has no attribute 'classify_priority_type'`

- [ ] **Step 3: Реализация**

Вставить в `bot/ticket_fields.py` после `classify_environment` (перед `ENV_UNDETERMINED_MSG`):

```python
async def classify_priority_type(
    history: str, ticket_title: str = ""
) -> tuple[str, str] | None:
    """(priority_id, type_id) по матрице комбинаций, или None если не определено.

    Один LLM-вызов на оба поля: приоритет и тип — одно решение.
    llama-3.3 (fast), фолбэк Llama 4 Scout. Keyword pre-pass не делаем:
    «блокирует/не блокирует торговлю» регулярками не различить.
    """
    if not history.strip() and not ticket_title.strip():
        return None
    parts: list[str] = []
    if ticket_title.strip():
        parts.append(f"Тема тикета: {ticket_title.strip()}")
    parts.append(f"Переписка:\n{history}")
    user_content = "\n\n".join(parts)
    prompt = _build_pt_prompt()
    raw = await _groq_classify(prompt, user_content)
    if raw is None:
        raw = await _groq_classify(prompt, user_content, model=_GROQ_FALLBACK_MODEL)
    if raw is None:
        return None
    combo = _parse_pt_combo(raw)
    if combo is None:
        logger.info("PT classifier: no valid combo in response %r", raw[:80])
    return combo
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: PASS (11 тестов)

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py bot/ticket_fields.py
git commit -m "feat(autofill): classify_priority_type LLM classifier"
```

---

### Task 3: HDE-клиент — top-level поля в PUT и чтение текущих значений

**Files:**
- Modify: `bot/hde_api.py` — метод `update_ticket_fields` (строка ~541) + новый метод рядом с `get_ticket_field_value` (строка ~199)
- Test: `tests/test_priority_type_autofill.py`

- [ ] **Step 1: Падающие тесты**

Дописать:

```python
# --- HDE client extension ---

def test_ticket_update_body_custom_only():
    from bot.hde_api import HDEApiClient
    assert HDEApiClient._ticket_update_body({"2": "145"}) == {"custom_fields": {"2": "145"}}


def test_ticket_update_body_with_priority_and_type():
    from bot.hde_api import HDEApiClient
    body = HDEApiClient._ticket_update_body({"2": "145"}, priority_id="1", type_id="0")
    # type_id=0 («Вопрос») falsy — обязан попасть в тело
    assert body == {"custom_fields": {"2": "145"}, "priority_id": 1, "type_id": 0}


def test_ticket_update_body_priority_only():
    from bot.hde_api import HDEApiClient
    body = HDEApiClient._ticket_update_body({}, priority_id="10")
    assert body == {"custom_fields": {}, "priority_id": 10}


@pytest.mark.asyncio
async def test_get_ticket_priority_type_parses_ids(monkeypatch):
    from bot.hde_api import HDEApiClient
    from unittest.mock import AsyncMock

    c = HDEApiClient.__new__(HDEApiClient)
    c.base_url = "https://x"
    c._get = AsyncMock(return_value=(200, {"data": {"priority_id": 10, "type_id": 0}}))
    assert await c.get_ticket_priority_type("T1") == ("10", "0")


@pytest.mark.asyncio
async def test_get_ticket_priority_type_error_returns_none(monkeypatch):
    from bot.hde_api import HDEApiClient
    from unittest.mock import AsyncMock

    c = HDEApiClient.__new__(HDEApiClient)
    c.base_url = "https://x"
    c._get = AsyncMock(return_value=(500, {}))
    assert await c.get_ticket_priority_type("T1") is None
```

- [ ] **Step 2: Прогнать — падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL, `AttributeError: ... '_ticket_update_body'`

- [ ] **Step 3: Реализация**

В `bot/hde_api.py` заменить `update_ticket_fields` (строки 541–550) на:

```python
    @staticmethod
    def _ticket_update_body(
        custom_fields: dict[str, str],
        priority_id: str | None = None,
        type_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"custom_fields": custom_fields}
        # type_id=0 («Вопрос») валиден — поэтому `is not None`, не truthiness
        if priority_id is not None:
            body["priority_id"] = int(priority_id)
        if type_id is not None:
            body["type_id"] = int(type_id)
        return body

    async def update_ticket_fields(
        self,
        ticket_id: str,
        custom_fields: dict[str, str],
        priority_id: str | None = None,
        type_id: str | None = None,
    ) -> HDEApiResult:
        """Update custom fields (keys = field IDs as strings) and, optionally,
        the standard priority_id/type_id of a ticket — one PUT for everything."""
        url = f"{self.base_url}/tickets/{ticket_id}/"
        body = self._ticket_update_body(custom_fields, priority_id, type_id)
        async with self._make_session() as session:
            async with session.put(url, json=body) as response:
                data = await self._read_response(response)
                if response.status >= 400:
                    message = self._extract_error_message(data) or f"HDE API error {response.status}"
                    raise HDEApiError(message)
                return HDEApiResult(status=response.status, data=data)
```

Добавить после `get_ticket_field_value` (после строки ~220):

```python
    async def get_ticket_priority_type(self, ticket_id: str) -> tuple[str, str] | None:
        """Current (priority_id, type_id) as strings, or None on any error.

        type_id may legitimately be "0" («Вопрос»)."""
        try:
            status, data = await self._get(f"{self.base_url}/tickets/{ticket_id}/")
            if status >= 400:
                return None
            raw = data.get("data", data) if isinstance(data, dict) else {}
            prio = raw.get("priority_id")
            typ = raw.get("type_id")
            return (
                str(prio) if prio is not None else "",
                str(typ) if typ is not None else "",
            )
        except Exception:
            return None
```

- [ ] **Step 4: Прогнать новые + существующие тесты клиента**

Run: `python -m pytest tests/test_priority_type_autofill.py tests/test_env_autofill.py -q`
Expected: PASS. Существующие вызовы `update_ticket_fields(ticket_id, {...})` совместимы (новые параметры опциональны).

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py bot/hde_api.py
git commit -m "feat(api): priority_id/type_id in ticket update PUT + current value read"
```

---

### Task 4: Колонки БД `priority_option_id` / `type_option_id`

**Files:**
- Modify: `bot/db/core.py` — `TICKET_TOPIC_COLUMNS` (строка ~84), `UPDATABLE_FIELDS` (строка ~109), dataclass `TicketTopic` (строка ~140), разбор row (строка ~602)
- Test: `tests/test_priority_type_autofill.py`

- [ ] **Step 1: Падающие тесты**

Дописать:

```python
# --- DB columns ---

@pytest.mark.asyncio
async def test_pt_option_ids_roundtrip():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    await _db.update_topic("t1", priority_option_id="1", type_option_id="0")
    rec = await _db.get_topic("t1")
    assert rec.priority_option_id == "1"
    assert rec.type_option_id == "0"


@pytest.mark.asyncio
async def test_pt_option_ids_default_none():
    await _db.init_db()
    await _db.upsert_topic("t1", 100)
    rec = await _db.get_topic("t1")
    assert rec.priority_option_id is None
    assert rec.type_option_id is None
```

- [ ] **Step 2: Прогнать — падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL (`update_topic` отвергает неизвестное поле либо AttributeError)

- [ ] **Step 3: Реализация в `bot/db/core.py`**

1. В `TICKET_TOPIC_COLUMNS` после `"env_option_id": "TEXT",`:

```python
    "priority_option_id": "TEXT",
    "type_option_id": "TEXT",
```

2. В `UPDATABLE_FIELDS` после `"env_option_id",`:

```python
    "priority_option_id",
    "type_option_id",
```

3. В dataclass `TicketTopic` после поля `env_option_id`:

```python
    # Приоритет/Тип autofill: None = не классифицировали, '' = не определено,
    # цифры = выставленный id (priority_id/type_id — top-level поля HDE)
    priority_option_id: Optional[str] = None
    type_option_id: Optional[str] = None
```

4. В разборе row (рядом со строкой `env_option_id=row[...]`, ~602), тем же паттерном:

```python
        priority_option_id=row["priority_option_id"] if "priority_option_id" in row.keys() else None,
        type_option_id=row["type_option_id"] if "type_option_id" in row.keys() else None,
```

Миграция не нужна: `init_db` добавляет недостающие колонки из `TICKET_TOPIC_COLUMNS` через ALTER (так появлялась `env_option_id`).

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/test_priority_type_autofill.py tests/test_topic_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py bot/db/core.py
git commit -m "feat(db): priority_option_id and type_option_id columns"
```

---

### Task 5: Подключение к `apply_ticket_fields()`

**Files:**
- Modify: `bot/ticket_fields.py` — `AutofillResult` (строка ~202), `apply_ticket_fields` (строки ~276–322)
- Test: `tests/test_priority_type_autofill.py`

- [ ] **Step 1: Падающие тесты**

Дописать (хелпер `_fake_record` скопировать из `tests/test_env_autofill.py`, он нужен и здесь):

```python
# --- apply_ticket_fields wiring ---

def _fake_record(photo_descriptions="", company_name="", env_option_id=None, ticket_name=""):
    from unittest.mock import MagicMock
    rec = MagicMock()
    rec.photo_descriptions = photo_descriptions
    rec.company_name = company_name
    rec.env_option_id = env_option_id
    rec.ticket_name = ticket_name
    return rec


def _patched_apply_env(monkeypatch, tf):
    """Общая обвязка: HDE-клиент, env-классификатор, БД-моки."""
    from unittest.mock import AsyncMock, MagicMock
    fake_client = MagicMock()
    fake_client.get_ticket_field_value = AsyncMock(return_value=199)
    fake_client.update_ticket_fields = AsyncMock()
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    monkeypatch.setattr(tf, "classify_environment", AsyncMock(return_value="146"))
    monkeypatch.setattr(_db, "get_topic", AsyncMock(return_value=None))
    update_topic = AsyncMock()
    monkeypatch.setattr(_db, "update_topic", update_topic)
    return fake_client, update_topic


@pytest.mark.asyncio
async def test_apply_sets_priority_and_type(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=("1", "3")))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: касса встала")

    assert res.priority_id == "1"
    assert res.type_id == "3"
    kwargs = fake_client.update_ticket_fields.await_args.kwargs
    assert kwargs["priority_id"] == "1"
    assert kwargs["type_id"] == "3"
    # предсказание сохранено в БД
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call and pt_call[0].kwargs["priority_option_id"] == "1"
    assert pt_call[0].kwargs["type_option_id"] == "3"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_pt_undetermined_warns_and_skips_fields(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: привет")

    assert res.priority_id is None and res.type_id is None
    kwargs = fake_client.update_ticket_fields.await_args.kwargs
    assert kwargs["priority_id"] is None
    assert kwargs["type_id"] is None
    # '' = «не определено» в БД
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call and pt_call[0].kwargs["priority_option_id"] == ""
    assert pt_call[0].kwargs["type_option_id"] == ""
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert any("Приоритет и тип не определены" in t for t in texts)


@pytest.mark.asyncio
async def test_apply_pt_type_vopros_zero_reaches_put(monkeypatch):
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client, update_topic = _patched_apply_env(monkeypatch, tf)
    monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=("3", "0")))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = await tf.apply_ticket_fields(bot, "T1", 555, "Клиент: как сделать X?")

    # «Вопрос» = "0" не должен потеряться из-за falsy-проверок
    assert res.type_id == "0"
    assert fake_client.update_ticket_fields.await_args.kwargs["type_id"] == "0"
    pt_call = [c for c in update_topic.await_args_list if "priority_option_id" in c.kwargs]
    assert pt_call[0].kwargs["type_option_id"] == "0"
```

- [ ] **Step 2: Прогнать — падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL (`AutofillResult` без `priority_id`; PUT без kwargs)

- [ ] **Step 3: Реализация**

1. В `AutofillResult` после `env_id`:

```python
    priority_id: str | None = None
    type_id: str | None = None
```

(и дополнить docstring класса строками `priority_id/type_id: выставленные значения, или None если не определено`).

2. Константа рядом с `ENV_UNDETERMINED_MSG`:

```python
PT_UNDETERMINED_MSG = "⚠️ Приоритет и тип не определены — выставьте вручную"
```

3. В `apply_ticket_fields` после блока «Окружение» (`env_id = ...` / `if env_id: ...`) добавить:

```python
    # Приоритет/Тип: однократно, при создании тикета; ручные правки
    # оператора потом не перезаписываются (повторных попыток нет).
    pt = await classify_priority_type(enriched, ticket_title)
    priority_id, type_id = pt if pt else (None, None)
```

4. PUT заменить с `await client.update_ticket_fields(ticket_id, fields)` на:

```python
        await client.update_ticket_fields(
            ticket_id, fields, priority_id=priority_id, type_id=type_id
        )
```

В обоих `return AutofillResult(...)` после PUT добавить `priority_id=priority_id, type_id=type_id`.

5. После сохранения `env_option_id` добавить (отдельным try — сбой БД не должен ронять поток):

```python
    try:
        await _db.update_topic(
            ticket_id,
            priority_option_id=priority_id if priority_id is not None else "",
            type_option_id=type_id if type_id is not None else "",
        )
    except Exception as exc:
        logger.warning("apply_ticket_fields: pt store failed for %s: %s", ticket_id, exc)
```

6. После блока предупреждения про окружение добавить аналогичный:

```python
    if pt is None:
        try:
            await bot.send_message(
                chat_id=config.group_chat_id,
                message_thread_id=topic_id,
                text=PT_UNDETERMINED_MSG,
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "apply_ticket_fields: pt warn-message failed for topic %s: %s",
                topic_id, exc,
            )
```

⚠️ Существующие тесты (`tests/test_env_autofill.py`, `tests/test_operator_replies.py` и пр.) зовут `apply_ticket_fields` без мока `classify_priority_type` — реальный вызов уйдёт в `_groq_classify`, который без `groq_api_key` вернёт None (в тестах ключа нет, это безопасно, но медленно/шумно). Проверить: если в существующих тестах появились лишние warn-сообщения (`bot.send_message`-ассерты), замокать `classify_priority_type` в них `AsyncMock(return_value=None)` НЕ нужно — вместо этого проверить ассерты: `test_apply_enriches_history_and_stores_env` использует `assert_awaited_once_with` только на `update_topic` — теперь вызовов два. Поправить эти тесты:
   - в `tests/test_env_autofill.py::test_apply_enriches_history_and_stores_env` заменить `update_topic.assert_awaited_once_with("T1", env_option_id="146")` на `update_topic.assert_any_await("T1", env_option_id="146")`, и добавить мок `monkeypatch.setattr(tf, "classify_priority_type", AsyncMock(return_value=None))`;
   - в `tests/test_env_autofill.py::test_apply_stores_empty_env_when_undetermined` — то же самое: `assert_any_await("T1", env_option_id="")` + мок;
   - в `tests/test_env_autofill.py::test_apply_appends_audio_transcripts` — добавить мок `classify_priority_type`.

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/test_priority_type_autofill.py tests/test_env_autofill.py tests/test_operator_replies.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py tests/test_env_autofill.py bot/ticket_fields.py
git commit -m "feat(autofill): wire priority/type classification into apply_ticket_fields"
```

---

### Task 6: Лог поправок операторов при закрытии тикета

**Files:**
- Modify: `bot/ticket_fields.py` (после `log_env_outcome`), `bot/topic_manager.py` (после строки ~761, рядом с вызовом `log_env_outcome`)
- Test: `tests/test_priority_type_autofill.py`

- [ ] **Step 1: Падающие тесты**

Дописать:

```python
# --- log_pt_outcome ---

@pytest.mark.asyncio
async def test_log_pt_outcome_writes_jsonl(tmp_path, monkeypatch):
    import json
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_priority_type = AsyncMock(return_value=("10", "2"))
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    out = tmp_path / "priority_corrections.jsonl"
    monkeypatch.setattr(tf, "PT_CORRECTIONS_PATH", str(out))

    await tf.log_pt_outcome("T1", "1", "3")

    entry = json.loads(out.read_text(encoding="utf-8").strip())
    assert entry["ticket_id"] == "T1"
    assert entry["predicted_priority"] == "1"
    assert entry["predicted_type"] == "3"
    assert entry["final_priority"] == "10"
    assert entry["final_type"] == "2"
    assert entry["match"] is False


@pytest.mark.asyncio
async def test_log_pt_outcome_match_with_type_zero(tmp_path, monkeypatch):
    import json
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock, MagicMock

    fake_client = MagicMock()
    fake_client.get_ticket_priority_type = AsyncMock(return_value=("3", "0"))
    monkeypatch.setattr(tf, "HDEApiClient", lambda: fake_client)
    out = tmp_path / "priority_corrections.jsonl"
    monkeypatch.setattr(tf, "PT_CORRECTIONS_PATH", str(out))

    await tf.log_pt_outcome("T1", "3", "0")

    entry = json.loads(out.read_text(encoding="utf-8").strip())
    assert entry["match"] is True


@pytest.mark.asyncio
async def test_log_pt_outcome_never_raises(monkeypatch):
    import bot.ticket_fields as tf

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(tf, "HDEApiClient", boom)
    await tf.log_pt_outcome("T1", "1", "3")  # не должно бросить


# --- topic_manager close hook ---

def _tm_payload(**overrides):
    payload = {
        "ticket_id": "TKT-1",
        "unique_id": "ABC-123",
        "ticket_name": "Касса не печатает",
        "company_name": "ACME",
        "priority": "high",
        "status": "open",
        "owner_id": "me",
        "owner_name": "Me",
        "user_name": "Alice",
        "message": "Помогите",
        "last_post_date": "2026-06-12 12:00:00",
        "link": "https://hde.example.com/tickets/1",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_ticket_closed_logs_pt_outcome(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)
    await _db.update_topic("TKT-1", priority_option_id="1", type_option_id="3")

    monkeypatch.setattr(tf, "log_env_outcome", AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(tf, "log_pt_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_awaited_once_with("TKT-1", "1", "3")


@pytest.mark.asyncio
async def test_ticket_closed_skips_pt_log_when_never_classified(monkeypatch):
    import bot.topic_manager as tm
    import bot.ticket_fields as tf
    from unittest.mock import AsyncMock

    await _db.init_db()
    await _db.upsert_topic("TKT-1", 999)  # priority_option_id остаётся NULL

    monkeypatch.setattr(tf, "log_env_outcome", AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(tf, "log_pt_outcome", log)
    monkeypatch.setattr(tm, "_delete_topic_now", AsyncMock(return_value=True))
    monkeypatch.setattr(tm, "_try_delete_pre_sla_message", AsyncMock())

    bot = AsyncMock()
    await tm.handle_ticket_closed(bot, _tm_payload(status="closed"))
    log.assert_not_awaited()
```

- [ ] **Step 2: Прогнать — падают**

Run: `python -m pytest tests/test_priority_type_autofill.py -x -q`
Expected: FAIL, `AttributeError: ... 'log_pt_outcome'`

- [ ] **Step 3: Реализация**

В `bot/ticket_fields.py` после `log_env_outcome` добавить:

```python
PT_CORRECTIONS_PATH = "data/priority_corrections.jsonl"


async def log_pt_outcome(
    ticket_id: str,
    predicted_priority: str | None,
    predicted_type: str | None,
) -> None:
    """При закрытии тикета фиксирует прогноз приоритета/типа vs финал.

    Оператор мог поправить поля вручную — JSONL даёт метрику точности.
    Never raises.
    """
    try:
        client = HDEApiClient()
        final = await client.get_ticket_priority_type(ticket_id)
        if final is None:
            return
        final_priority, final_type = final
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticket_id": ticket_id,
            "predicted_priority": predicted_priority,
            "predicted_type": predicted_type,
            "final_priority": final_priority or None,
            "final_type": final_type if final_type != "" else None,
            "match": (
                predicted_priority is not None
                and predicted_type is not None
                and final_priority == predicted_priority
                and final_type == predicted_type
            ),
        }
        os.makedirs(os.path.dirname(PT_CORRECTIONS_PATH) or ".", exist_ok=True)
        with open(PT_CORRECTIONS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("log_pt_outcome failed for %s: %s", ticket_id, exc)
```

В `bot/topic_manager.py` сразу после существующего блока (строки 759–761):

```python
    if record.env_option_id is not None:
        from .ticket_fields import log_env_outcome
        await log_env_outcome(ticket_id, record.env_option_id or None)
```

добавить:

```python
    if record.priority_option_id is not None:
        from .ticket_fields import log_pt_outcome
        await log_pt_outcome(
            ticket_id,
            record.priority_option_id or None,
            record.type_option_id or None,
        )
```

(`'' or None` → None: «классифицировали, но не определили» логируется с predicted=None — считается несовпадением, это намеренно.)

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/test_priority_type_autofill.py tests/test_env_autofill.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_priority_type_autofill.py bot/ticket_fields.py bot/topic_manager.py
git commit -m "feat(autofill): log predicted vs final priority/type on ticket close"
```

---

### Task 7: Полный прогон и финальная проверка

**Files:** нет новых.

- [ ] **Step 1: Полный тестовый прогон**

Run: `python -m pytest tests/ -q`
Expected: все тесты зелёные (321 старый + ~21 новый). Если что-то красное — чинить до зелёного, не коммитить красное.

- [ ] **Step 2: Самопроверка против спеки**

Свериться со спекой `docs/superpowers/specs/2026-06-12-priority-type-autofill-design.md`:
- матрица 6 комбинаций в промпте — Task 1;
- один LLM-вызов, фолбэк Scout — Task 2;
- один PUT на все поля — Task 3 + Task 5;
- однократность (нет retry, нет перезаписи) — в `apply_ticket_fields` нет повторного входа: вызывается только из `bot/topic_history.py:269` и `bot/handlers/commands.py:132` (ручная команда — допустимо);
- «не определено» → ничего не ставим + ⚠️ — Task 5;
- метрика поправок — Task 6.

- [ ] **Step 3: Commit остатков (если есть)**

```bash
git status
git add -A docs/superpowers/plans/2026-06-12-priority-type-autofill.md
git commit -m "docs: priority/type autofill plan marked complete"
```

---

## Заметки для исполнителя

- **`type_id` «Вопрос» = 0.** Главная ловушка фичи. Все проверки — `is not None`, в БД хранится строка `"0"` (truthy), в PUT уходит `int("0") == 0`.
- **Никаких изменений в `retry_env_classification`** — повторная классификация есть только у окружения, у приоритета/типа её нет (решение пользователя).
- **Не трогать** существующие приоритеты «1я линия основной» (id 2), «Стандарт 2я» (6), «ИИ» (5) и тип «Инцидент» (1) — бот их никогда не выставляет.
- В тестах нет реальных вызовов Groq/HDE: всё мокается, ключей в окружении CI нет.
- Деплой после мержа — стандартный: push в main, затем SSH pull + restart на VPS (как описано в memory `project_deploy_vps.md`); прогнать руками один тикет и посмотреть лог.

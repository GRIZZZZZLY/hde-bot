# Prompt Optimizer — Design Spec

**Date:** 2026-04-13
**Status:** Approved

## Goal

Система автономной ночной оптимизации промптов: несколько LLM-моделей предлагают мутации `_FORMAT_INSTRUCTIONS`, лучшая оценивается на исторических тикетах, утром оператор получает Telegram-отчёт с кнопками "Применить" / "Отклонить".

## Context

- Бот использует Gemini 2.5 Flash для генерации AI-подсказок
- Промпт (`_FORMAT_INSTRUCTIONS`) написан вручную и никогда не обновляется
- Оператор даёт неявную обратную связь через 👍/✏️/👎/📤 — эти данные нигде не накапливаются системно
- Цель: замкнуть петлю обратной связи и превратить фидбек оператора в улучшение качества ответов

## Architecture

```
bot/optimizer/
  __init__.py
  agent.py          — главный цикл: датасет → мутации → оценка → отчёт
  evaluator.py      — replay тикетов, вычисление combined score
  mutations.py      — промпт для LLM "предложи улучшение FORMAT_INSTRUCTIONS"
  llm_router.py     — GeminiClient, GroqClient, LLMRouter

bot/db.py           — +optimization_samples, +prompt_versions таблицы
bot/ai_summary.py   — читает активный промпт из prompt_versions при старте
bot/scheduler.py    — триггер в 23:00 UTC (02:00 МСК)
bot/handlers/commands.py — callbacks: opt_apply, opt_reject, opt_detail
bot/config.py       — +groq_api_key
```

**Tech Stack:** Python 3.10+, aiosqlite, httpx (уже есть), Gemini API (уже есть), Groq API (новый)

---

## Component 1 — Схема данных

### Таблица `optimization_samples`

Заполняется в `topic_manager.py` при implicit feedback — одна запись на тикет с известным исходом.

```sql
CREATE TABLE IF NOT EXISTS optimization_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    title       TEXT,
    history     TEXT NOT NULL,
    ai_answer   TEXT NOT NULL,
    op_answer   TEXT,
    outcome     TEXT NOT NULL,  -- 'sent' | 'accepted' | 'corrected' | 'rejected'
    confidence  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
```

Веса исходов для метрики:
- `'sent'` → 1.0 (📤 отправлен напрямую)
- `'accepted'` → 0.8 (👍 одобрен)
- `'corrected'` → 0.4 (✏️ исправлен, есть op_answer для similarity)
- `'rejected'` → 0.0 (👎 отклонён)

### Таблица `prompt_versions`

```sql
CREATE TABLE IF NOT EXISTS prompt_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    score       REAL,
    proposed_by TEXT,           -- 'gemini' | 'llama' | 'mixtral'
    status      TEXT NOT NULL,  -- 'active' | 'candidate' | 'rejected'
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at  TEXT
)
```

При старте `ai_summary.py` вызывает `db.get_active_prompt()` — если есть запись `status='active'`, использует её вместо захардкоженного `_FORMAT_INSTRUCTIONS`.

---

## Component 2 — Multi-LLM Router

Файл: `bot/optimizer/llm_router.py`

```python
class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...

class GeminiClient:   # обёртка над существующей логикой из ai_summary.py
class GroqClient:     # httpx + Groq REST API (OpenAI-совместимый)

class LLMRouter:
    clients = {
        "gemini":  GeminiClient(model="gemini-2.5-flash"),
        "llama":   GroqClient(model="llama-3.3-70b-versatile"),
        "mixtral": GroqClient(model="mixtral-8x7b-32768"),
    }

    async def complete_all(self, system: str, user: str) -> dict[str, str]:
        """Параллельный запрос всех активных моделей. Возвращает dict model→ответ."""
```

**Groq API:** `base_url = "https://api.groq.com/openai/v1/chat/completions"`, OpenAI-совместимый формат.
Новая env переменная: `GROQ_API_KEY`. В `config.py`: `groq_api_key: str = ""` — пустая строка = Groq недоступен, роутер пропускает клиент.

**Изоляция:** `LLMRouter` используется только внутри `bot/optimizer/`. Существующий `ai_summary.py` продолжает напрямую вызывать Gemini.

---

## Component 3 — Цикл оптимизации

Файл: `bot/optimizer/agent.py`

Запускается из `process_scheduled_actions()` при `now.hour == 23 and now.minute < 1`.

```
1. Загрузить датасет: последние 30 дней из optimization_samples
   Минимум 10 записей — иначе пропустить ("недостаточно данных")

2. Вычислить baseline_score на текущем промпте (replay датасета)

3. Параллельно запросить мутации у всех трёх моделей через LLMRouter.complete_all()
   Системный промпт (из mutations.py):
     "Вот инструкция для AI-ассистента техподдержки кассового оборудования.
      Вот примеры где оператор принял ответ и где отклонил.
      Предложи улучшенную версию инструкции. Верни только текст инструкции."

4. Для каждой из 3 мутаций — оценить на датасете через evaluator.py

5. Выбрать победителя: max(score) > baseline_score + 0.03
   Прирост < 3% = "улучшений не найдено" (сохранить в лог, не беспокоить оператора)

6. Сохранить все 3 кандидата в prompt_versions (status='candidate')

7. Отправить Telegram-отчёт оператору
```

### Combined Score (`evaluator.py`)

```python
def combined_score(samples, prompt_content) -> float:
    acceptance_score = weighted_acceptance_rate(samples, prompt_content)
    similarity_score = avg_cosine_similarity(corrected_samples, prompt_content)
    return 0.7 * acceptance_score + 0.3 * similarity_score
```

- `weighted_acceptance_rate`: для каждого sample генерируем ответ с новым промптом, сравниваем с `op_answer` через `difflib.SequenceMatcher` ratio ≥ 0.7 → "принят". Умножаем на вес исхода.
- `avg_cosine_similarity`: только для `outcome='corrected'` сэмплов. Cosine similarity между сгенерированным ответом и тем что отправил оператор (через sentence-transformers, уже в проекте).

---

## Component 4 — Telegram-отчёт

Формат сообщения:

```
🧪 Ночная оптимизация промпта

📊 Данные: 47 тикетов · 30 дней
⚡ Сейчас: 71 балл

🥇 Победитель: llama-3.3-70b
📈 Результат: 77 баллов (+8%)

📝 Предложенное изменение:
— было: «Дай краткий ответ специалисту...»
+ стало: «Дай ответ в формате: Диагноз → Шаги...»

[✅ Применить]  [❌ Отклонить]  [📊 Подробнее]
```

**Кнопка 📊 Подробнее:** второе сообщение — все 3 кандидата со скорами + пример ответа каждой модели на конкретный тикет из датасета.

**Callback handlers в `bot/handlers/commands.py`:**
- `opt_apply:{version_id}` → активировать победителя (`status='active'`), остальные `'rejected'`; редактировать сообщение "✅ Применено"
- `opt_reject:{version_id}` → все кандидаты `'rejected'`; редактировать "❌ Отклонено"
- `opt_detail:{version_id}` → отправить детальный отчёт

Также добавить `/aioptimize` команду для ручного запуска оптимизатора (для тестирования без ожидания ночи).

---

## Изменения в существующих файлах

| Файл | Изменение |
|---|---|
| `bot/db.py` | +`optimization_samples`, +`prompt_versions`, +`save_optimization_sample()`, +`get_active_prompt()` |
| `bot/topic_manager.py` | В `_implicit_feedback` и callback-хендлерах: сохранять запись в `optimization_samples` |
| `bot/ai_summary.py` | `get_active_format_instructions()` — читает из БД лениво, кэширует в памяти |
| `bot/scheduler.py` | +`_last_optimization_date`, триггер 23:00 UTC, `asyncio.create_task(_run_optimizer(bot))` |
| `bot/handlers/commands.py` | +`cb_opt_apply`, +`cb_opt_reject`, +`cb_opt_detail`, +`cmd_aioptimize` |
| `bot/main.py` | +`BotCommand("aioptimize", "Запустить оптимизацию промпта вручную")` |
| `bot/config.py` | +`groq_api_key: str = ""` из `GROQ_API_KEY` |

---

## Что НЕ входит в этот спек

- Оптимизация `_build_system_prompt` (только `_FORMAT_INSTRUCTIONS`)
- Авто-применение без одобрения оператора
- Оптимизация RAG-параметров (порог cosine, веса BM25/RRF)
- Добавление других LLM провайдеров (Claude, GPT-4o)

---

## Проверка

1. `GROQ_API_KEY` добавлен в `.env`, перезапуск → нет ошибок инициализации в логах
2. Нажать 👍/👎/✏️ на AI-ответ → запись появилась в `optimization_samples`
3. `/aioptimize` → приходит Telegram-отчёт (или "недостаточно данных" если < 10 сэмплов)
4. Нажать ✅ Применить → следующий тикет использует новый промпт
5. Нажать 📊 Подробнее → второе сообщение с тремя кандидатами
6. При прирост < 3% → оператор не получает уведомление, только лог

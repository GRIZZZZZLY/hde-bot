# Offline Eval Harness + LLM-Judge + Human Style Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Офлайн eval-харнесс для промтов на реальных данных с VPS, LLM-judge с критерием «звучит как человек», профиль голоса оператора и holdout-оценка в ночном оптимизаторе.

**Architecture:** Два контура над общими артефактами SQLite (`prompt_versions`, `optimization_samples`): локальный CLI-харнесс (replay тикетов + двухслойный скоринг) и апгрейд ночного `/aioptimize` (мутации Gemini-first, apply только по holdout). Новые модули: `bot/optimizer/judge.py`, `bot/optimizer/dataset.py`, `bot/voice_profile.py`. Спека: `docs/superpowers/specs/2026-06-11-offline-eval-harness-design.md`.

**Tech Stack:** Python 3.11+, aiohttp, sqlite3/aiosqlite, pytest (анбуферизованный `python -m pytest`), Gemini 2.5 Flash API (ключ `GEMINI_API_KEY`), существующий `LLM_SEMAPHORE`.

**Контекст кодовой базы (прочитать перед стартом):**
- `bot/optimizer/evaluator.py` — `combined_score(samples, format_instructions, *, _generate_fn)`, внутри генерит ответы Gemini и меряет similarity. `_generate_fn` инжектится для тестов.
- `bot/optimizer/agent.py` — ночной цикл: samples → baseline → мутации (`LLMRouter.complete_all`) → скоринг → отчёт с кнопками apply/reject.
- `bot/optimizer/llm_router.py` — сейчас Groq-first, Gemini fallback. Мы инвертируем.
- `bot/db.py:1936+` — `get_optimization_samples(days)`, `get_active_prompt()`, `save_prompt_version()`, `apply_prompt_version()`. `DB_PATH = "hde_bot.db"` (относительный, cwd).
- `bot/ai_summary.py:224-279` — `_build_system_prompt()`, few-shot блок на строках 257-270.
- Таблица `optimization_samples`: `id, ticket_id, title, history, ai_answer, op_answer, outcome ('sent'|'accepted'|'corrected'|'rejected'), confidence, created_at`. `op_answer` пишет сам владелец через кнопку ✏️ — это и есть «голос оператора», фильтр по owner НЕ нужен.
- VPS: хост-алиас `config1`, путь `/opt/hde-bot`, сервис `hde-bot` (см. `scripts/export_prompt_samples.py` docstring).
- Тесты запускать: `python -m pytest tests/<file> -v` из корня `d:\HDE_bot`.

---

### Task 1: Скрипт переноса базы с VPS

**Files:**
- Create: `scripts/pull_kb.sh`
- Modify: `.gitignore` (добавить `data/`)

- [ ] **Step 1: Написать скрипт**

```bash
#!/usr/bin/env bash
# Скачивает рабочую базу бота с VPS для офлайн-анализа и eval-харнесса.
# Usage: bash scripts/pull_kb.sh
set -euo pipefail

mkdir -p data
scp config1:/opt/hde-bot/hde_bot.db data/hde_bot_vps.db
python - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/hde_bot_vps.db")
n_samples = conn.execute("SELECT COUNT(*) FROM optimization_samples").fetchone()[0]
n_prompts = conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0]
n_kb = conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
print(f"OK: optimization_samples={n_samples}, prompt_versions={n_prompts}, knowledge_items={n_kb}")
EOF
```

- [ ] **Step 2: Добавить `data/` в .gitignore**

Открыть `.gitignore`, добавить строку `data/` (если её нет).

- [ ] **Step 3: Проверить вручную**

Run: `bash scripts/pull_kb.sh`
Expected: `OK: optimization_samples=<N>, prompt_versions=<M>, knowledge_items=<K>` с ненулевыми числами. Если scp падает по ssh-ключу — сообщить пользователю, не блокировать остальные задачи (они работают и без свежей базы).

- [ ] **Step 4: Commit**

```bash
git add scripts/pull_kb.sh .gitignore
git commit -m "feat(eval): add pull_kb.sh to fetch production DB from VPS"
```

---

### Task 2: Русский анти-ИИ стайлгайд

**Files:**
- Create: `bot/prompts/style_guide_ru.md`

- [ ] **Step 1: Создать файл с полным содержимым**

```markdown
# Стайлгайд: человеческий стиль ответов клиенту

Применяется в первую очередь к полю «Клиенту». Цель: ответ неотличим
от сообщения живого оператора поддержки кассового оборудования.

## Запрещено

**Чат-вежливость и шаблоны:**
- «Надеюсь, это поможет», «Если возникнут вопросы — обращайтесь», «Спасибо за обращение»
- «Отличный вопрос!», «Конечно!», «Разумеется!», «Безусловно!»
- Приветствия («Добрый день!», «Здравствуйте!») — сразу по делу
- Пустые концовки («Хорошего дня!», «Всегда рады помочь», «Обращайтесь ещё»)

**ИИ-канцелярит:**
- «является», «данный», «осуществить», «производится», «функционирует»
- «в рамках», «на сегодняшний день», «важно отметить», «следует учитывать»
- «в случае возникновения» → «если»; «произвести оплату» → «оплатить»

**Пассив без актора:**
- «должна быть произведена перезагрузка» → «перезагрузите кассу»
- «настройка будет выполнена» → «настроим» / «настройте»

**ИИ-структуры:**
- «не просто X, но и Y», «это не X, а Y» — утверждать Y сразу
- Навязчивые тройки («быстро, удобно и надёжно»)
- Списки там, где хватит одного предложения
- «Давайте разберёмся», «Выполните следующие шаги:» — сразу описывать действия

**Hedging-стопки:**
- «возможно, вероятно, может быть» подряд — одно слово или конкретика

## Обязательно

- Конкретика: модель устройства, название кнопки/пункта меню, точный срок
- «вы» и императив («проверьте», «пришлите»), а не «пользователю необходимо»
- Короткие фразы, как в живой переписке
- Тире по правилам русского языка — это НЕ маркер ИИ, не избегать
```

- [ ] **Step 2: Commit**

```bash
git add bot/prompts/style_guide_ru.md
git commit -m "feat(style): add Russian anti-AI style guide distilled from stop-slop/humanizer"
```

---

### Task 3: Детерминированный train/holdout split

**Files:**
- Create: `bot/optimizer/dataset.py`
- Test: `tests/test_optimizer_dataset.py`

- [ ] **Step 1: Написать падающий тест**

```python
"""Tests for deterministic train/holdout split."""
from bot.optimizer.dataset import HOLDOUT_RATIO, is_holdout, split_samples


def test_is_holdout_deterministic():
    assert is_holdout(42) == is_holdout(42)
    assert is_holdout("42") == is_holdout(42)  # str/int одинаково


def test_split_ratio_approx():
    samples = [{"id": i} for i in range(1000)]
    train, holdout = split_samples(samples)
    assert 0.12 <= len(holdout) / 1000 <= 0.28  # ~20% с допуском
    assert len(train) + len(holdout) == 1000


def test_split_stable_across_calls():
    samples = [{"id": i} for i in range(100)]
    _, h1 = split_samples(samples)
    _, h2 = split_samples(samples)
    assert [s["id"] for s in h1] == [s["id"] for s in h2]


def test_split_empty():
    assert split_samples([]) == ([], [])
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `python -m pytest tests/test_optimizer_dataset.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'bot.optimizer.dataset'`

- [ ] **Step 3: Минимальная реализация**

```python
"""Детерминированный train/holdout split для optimization_samples.

Holdout стабилен: один и тот же id всегда попадает в одну корзину,
независимо от размера выборки и порядка. Это лечит переобучение
оптимизатора: apply-решение принимается по сэмплам, на которых
мутации не настраивались.
"""
from __future__ import annotations

import hashlib

HOLDOUT_RATIO = 0.2


def is_holdout(sample_id: int | str, ratio: float = HOLDOUT_RATIO) -> bool:
    digest = hashlib.sha1(str(sample_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:2], "big") / 65535.0
    return bucket < ratio


def split_samples(
    samples: list[dict], ratio: float = HOLDOUT_RATIO
) -> tuple[list[dict], list[dict]]:
    """Returns (train, holdout)."""
    train: list[dict] = []
    holdout: list[dict] = []
    for s in samples:
        key = s.get("id", s.get("ticket_id", ""))
        (holdout if is_holdout(key, ratio) else train).append(s)
    return train, holdout
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `python -m pytest tests/test_optimizer_dataset.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add bot/optimizer/dataset.py tests/test_optimizer_dataset.py
git commit -m "feat(optimizer): deterministic train/holdout split"
```

---

### Task 4: Параметр max_samples в evaluator

**Files:**
- Modify: `bot/optimizer/evaluator.py:59-78`
- Test: `tests/test_optimizer_dataset.py` (дописать в конец)

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_optimizer_dataset.py`:

```python
import pytest

from bot.optimizer.evaluator import combined_score


@pytest.mark.asyncio
async def test_combined_score_max_samples_none_evaluates_all():
    calls = []

    async def fake_generate(history, title, instructions):
        calls.append(history)
        return "Клиенту: перезагрузите кассу"

    samples = [
        {"id": i, "ticket_id": str(i), "title": "t", "history": f"h{i}",
         "ai_answer": "a", "op_answer": "Клиенту: перезагрузите кассу",
         "outcome": "corrected"}
        for i in range(25)
    ]
    await combined_score(samples, "инструкция", max_samples=None, _generate_fn=fake_generate)
    assert len(calls) == 25  # не обрезано до дефолтных 20
```

Если `pytest.mark.asyncio` не работает — посмотреть, как async-тесты оформлены в `tests/test_prompt_optimizer.py`, и повторить тот же паттерн (там уже есть async-тесты evaluator).

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python -m pytest tests/test_optimizer_dataset.py -v -k max_samples`
Expected: FAIL с `TypeError: combined_score() got an unexpected keyword argument 'max_samples'`

- [ ] **Step 3: Реализация**

В `bot/optimizer/evaluator.py` изменить сигнатуру и обрезку:

```python
async def combined_score(
    samples: list[dict],
    format_instructions: str,
    *,
    max_samples: int | None = _MAX_EVAL_SAMPLES,
    _generate_fn: GenerateFn | None = None,
) -> float:
```

и заменить строку
`eval_set = samples if len(samples) <= _MAX_EVAL_SAMPLES else random.sample(samples, _MAX_EVAL_SAMPLES)`
на:

```python
    if max_samples is not None and len(samples) > max_samples:
        eval_set = random.sample(samples, max_samples)
    else:
        eval_set = samples
```

Docstring дополнить строкой: `max_samples: обрезка выборки для экономии API; None = оценивать все детерминированно.`

- [ ] **Step 4: Запустить тесты evaluator + новые**

Run: `python -m pytest tests/test_optimizer_dataset.py tests/test_prompt_optimizer.py tests/test_prompt_optimization.py -v`
Expected: all passed (существующие вызовы без `max_samples` сохраняют старое поведение)

- [ ] **Step 5: Commit**

```bash
git add bot/optimizer/evaluator.py tests/test_optimizer_dataset.py
git commit -m "feat(optimizer): combined_score max_samples param for full deterministic eval"
```

---

### Task 5: LLM-judge

**Files:**
- Create: `bot/optimizer/judge.py`
- Test: `tests/test_judge.py`

- [ ] **Step 1: Написать падающие тесты**

```python
"""Tests for LLM-judge."""
import json

import pytest

from bot.optimizer.judge import build_judge_prompt, holdout_score, judge_answer

_GOOD_VERDICT = json.dumps({
    "diagnosis_correct": True, "equipment_named": True, "format_ok": True,
    "length_ok": True, "sounds_human": False, "overall": 6,
    "reason": "шаблонная вежливость в конце",
})


def test_build_judge_prompt_contains_inputs_and_style_guide():
    system, user = build_judge_prompt(
        history="Касса Атол 30Ф не печатает чек",
        title="Не печатает",
        candidate="Клиенту: перезагрузите кассу",
        op_answer="Перезагрузите кассу, проверьте ленту",
    )
    assert "Атол 30Ф" in user
    assert "перезагрузите кассу" in user.lower()
    assert "Памятка" in system or "Клиенту" in system  # формат описан
    assert "канцелярит" in system.lower() or "Запрещено" in system  # стайлгайд вшит


@pytest.mark.asyncio
async def test_judge_answer_parses_json():
    async def fake_call(system, user):
        return _GOOD_VERDICT

    verdict = await judge_answer("h", "t", "cand", "ref", _call_fn=fake_call)
    assert verdict["overall"] == 6
    assert verdict["sounds_human"] is False


@pytest.mark.asyncio
async def test_judge_answer_retries_once_then_none():
    calls = []

    async def bad_call(system, user):
        calls.append(1)
        return "это не json"

    verdict = await judge_answer("h", "t", "cand", "ref", _call_fn=bad_call)
    assert verdict is None
    assert len(calls) == 2  # один повтор


@pytest.mark.asyncio
async def test_holdout_score_formula():
    async def fake_generate(history, title, instructions):
        return "Клиенту: перезагрузите кассу"

    async def fake_judge(history, title, candidate, op_answer):
        return json.loads(_GOOD_VERDICT)  # overall=6 → 0.6

    samples = [
        {"id": 1, "ticket_id": "1", "title": "t", "history": "h",
         "ai_answer": "a", "op_answer": "Клиенту: перезагрузите кассу",
         "outcome": "corrected"},
    ]
    score = await holdout_score(
        samples, "инструкция", _generate_fn=fake_generate, _judge_fn=fake_judge
    )
    # combined_score даст ~1.0 (точное совпадение), judge 0.6 → 0.5*1.0+0.5*0.6=0.8
    assert 0.75 <= score <= 0.85


@pytest.mark.asyncio
async def test_holdout_score_without_op_answers_falls_back_to_base():
    async def fake_generate(history, title, instructions):
        return "Клиенту: ответ"

    async def fake_judge(history, title, candidate, op_answer):
        raise AssertionError("judge не должен вызываться без op_answer")

    samples = [
        {"id": 1, "ticket_id": "1", "title": "t", "history": "h",
         "ai_answer": "", "op_answer": None, "outcome": "accepted"},
    ]
    score = await holdout_score(
        samples, "инструкция", _generate_fn=fake_generate, _judge_fn=fake_judge
    )
    assert 0.0 <= score <= 1.0
```

(паттерн async-тестов взять из `tests/test_prompt_optimizer.py`, если `pytest.mark.asyncio` не настроен)

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `python -m pytest tests/test_judge.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'bot.optimizer.judge'`

- [ ] **Step 3: Реализация**

```python
"""LLM-as-judge: оценка ответа кандидата против реального ответа оператора.

Используется офлайн-харнессом (scripts/eval_prompt.py) и ночным
оптимизатором (agent.py) для финальной оценки на holdout-наборе.
Gemini 2.5 Flash, temperature=0, structured output (responseSchema).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from ..llm_semaphore import LLM_SEMAPHORE
from .evaluator import combined_score, _generate_answer

logger = logging.getLogger(__name__)

_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "diagnosis_correct": {"type": "BOOLEAN"},
        "equipment_named": {"type": "BOOLEAN"},
        "format_ok": {"type": "BOOLEAN"},
        "length_ok": {"type": "BOOLEAN"},
        "sounds_human": {"type": "BOOLEAN"},
        "overall": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
    },
    "required": [
        "diagnosis_correct", "equipment_named", "format_ok",
        "length_ok", "sounds_human", "overall", "reason",
    ],
}

CallFn = Callable[[str, str], Awaitable[str]]
JudgeFn = Callable[[str, str, str, str], Awaitable[dict | None]]
GenerateFn = Callable[[str, str, str], Awaitable[str]]


def _load_style_guide() -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / "style_guide_ru.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_judge_prompt(
    history: str, title: str, candidate: str, op_answer: str
) -> tuple[str, str]:
    """Returns (system, user) для запроса к судье."""
    system = (
        "Ты строгий судья качества ответов AI-ассистента поддержки кассового "
        "оборудования (АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).\n"
        "Ответ ассистента имеет формат «Суть / Клиенту / Памятка».\n\n"
        "Сравни ответ кандидата с реальным ответом оператора и оцени:\n"
        "- diagnosis_correct: верно ли понята причина проблемы (сверяй с оператором)\n"
        "- equipment_named: названа ли модель оборудования, если она есть в истории\n"
        "- format_ok: есть ли секция «Клиенту:», нет ли лишних секций\n"
        "- length_ok: поле «Клиенту» не длиннее ~20 слов, без воды\n"
        "- sounds_human: текст для клиента звучит как живой оператор, "
        "БЕЗ перечисленных ниже ИИ-паттернов\n"
        "- overall: целое 0-10, общая оценка\n"
        "- reason: одна фраза, что главное не так (или «ок»)\n\n"
        "Стайлгайд для sounds_human:\n"
        f"{_load_style_guide()}"
    )
    user = (
        f"Тема тикета: {title}\n\n"
        f"История (фрагмент):\n{history[-2000:]}\n\n"
        f"Ответ кандидата:\n{candidate}\n\n"
        f"Реальный ответ оператора:\n{op_answer}"
    )
    return system, user


async def _call_gemini(system: str, user: str) -> str:
    from ..config import config

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 500,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }
    async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
        async with session.post(
            _URL,
            params={"key": config.gemini_api_key},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
    candidates = data.get("candidates")
    if not candidates:
        error = data.get("error", {})
        msg = error.get("message") if isinstance(error, dict) else str(data)
        raise RuntimeError(f"Gemini judge returned no candidates: {msg}")
    return candidates[0]["content"]["parts"][0]["text"].strip()


async def judge_answer(
    history: str,
    title: str,
    candidate: str,
    op_answer: str,
    *,
    _call_fn: CallFn | None = None,
) -> dict | None:
    """Вердикт судьи или None (после одного повтора при битом JSON/ошибке)."""
    call = _call_fn or _call_gemini
    system, user = build_judge_prompt(history, title, candidate, op_answer)
    for attempt in (1, 2):
        try:
            raw = await call(system, user)
            verdict = json.loads(raw)
            if not isinstance(verdict.get("overall"), int):
                raise ValueError("overall is not int")
            return verdict
        except Exception as exc:
            logger.warning("Judge attempt %d failed: %s", attempt, exc)
    return None


async def holdout_score(
    samples: list[dict],
    format_instructions: str,
    *,
    _generate_fn: GenerateFn | None = None,
    _judge_fn: JudgeFn | None = None,
) -> float:
    """Финальный скор на holdout: 0.5 * combined_score + 0.5 * judge.

    Генерация мемоизируется, чтобы combined_score и судья не дёргали
    Gemini дважды за один сэмпл. Сэмплы без op_answer судьёй не
    оцениваются; если таких нет вовсе — возвращается чистый combined_score.
    """
    raw_generate = _generate_fn or _generate_answer
    judge = _judge_fn or judge_answer

    memo: dict[tuple[str, str], str] = {}

    async def generate(history: str, title: str, instructions: str) -> str:
        key = (history, title)
        if key not in memo:
            memo[key] = await raw_generate(history, title, instructions)
        return memo[key]

    base = await combined_score(
        samples, format_instructions, max_samples=None, _generate_fn=generate
    )

    judge_scores: list[float] = []
    for s in samples:
        ref = s.get("op_answer")
        if not ref:
            continue
        try:
            answer = await generate(s.get("history", ""), s.get("title", ""), format_instructions)
            verdict = await judge(s.get("history", ""), s.get("title", ""), answer, ref)
        except Exception as exc:
            logger.warning("Judge failed for sample %s: %s", s.get("ticket_id"), exc)
            continue
        if verdict is not None:
            judge_scores.append(min(max(verdict["overall"], 0), 10) / 10.0)

    if not judge_scores:
        return base
    return 0.5 * base + 0.5 * (sum(judge_scores) / len(judge_scores))
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `python -m pytest tests/test_judge.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add bot/optimizer/judge.py tests/test_judge.py
git commit -m "feat(optimizer): LLM-judge with sounds_human criterion and holdout_score"
```

---

### Task 6: Модуль профиля голоса + скрипт сборки

**Files:**
- Create: `bot/voice_profile.py`
- Create: `scripts/build_voice_profile.py`
- Test: `tests/test_voice_profile.py`

- [ ] **Step 1: Написать падающие тесты**

```python
"""Tests for voice profile extraction and loading."""
import json

from bot.voice_profile import extract_voice_examples, load_voice_examples


def test_extract_filters_by_length():
    texts = ["коротко", "х" * 700, "Перезагрузите кассу и проверьте чековую ленту, после этого пробейте тестовый чек"]
    out = extract_voice_examples(texts)
    assert len(out) == 1
    assert out[0].startswith("Перезагрузите")


def test_extract_dedupes_case_insensitive():
    t = "Перезагрузите кассу и проверьте чековую ленту, затем пробейте тестовый чек"
    out = extract_voice_examples([t, t.upper()])
    assert len(out) == 1


def test_extract_caps_at_max():
    texts = [
        f"Проверьте подключение кассы номер {i} к сети и перезапустите драйвер ККТ"
        for i in range(20)
    ]
    out = extract_voice_examples(texts, max_examples=8)
    assert len(out) == 8


def test_extract_normalizes_whitespace():
    out = extract_voice_examples(["Проверьте   подключение\n\nкассы к сети и перезапустите драйвер ККТ"])
    assert "  " not in out[0]
    assert "\n" not in out[0]


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    import bot.voice_profile as vp
    monkeypatch.setattr(vp, "_PROFILE_PATH", tmp_path / "nope.json")
    assert load_voice_examples() == []


def test_load_reads_examples(tmp_path, monkeypatch):
    import bot.voice_profile as vp
    p = tmp_path / "voice_profile.json"
    p.write_text(json.dumps({"examples": ["пример один", 42]}), encoding="utf-8")
    monkeypatch.setattr(vp, "_PROFILE_PATH", p)
    assert load_voice_examples() == ["пример один"]
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `python -m pytest tests/test_voice_profile.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'bot.voice_profile'`

- [ ] **Step 3: Реализация `bot/voice_profile.py`**

```python
"""Профиль голоса оператора: реальные ответы клиентам как образец стиля.

Источник — optimization_samples.op_answer (тексты, которые оператор сам
писал/правил через кнопку ✏️). Скрипт scripts/build_voice_profile.py
собирает примеры в bot/prompts/voice_profile.json; ai_summary подмешивает
их в системный промт.
"""
from __future__ import annotations

import json
from pathlib import Path

_PROFILE_PATH = Path(__file__).parent / "prompts" / "voice_profile.json"
_MIN_LEN = 40
_MAX_LEN = 600


def extract_voice_examples(texts: list[str], max_examples: int = 8) -> list[str]:
    """Отбирает характерные ответы: нормализует пробелы, режет по длине, дедупит."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        t = " ".join((text or "").split())
        if not (_MIN_LEN <= len(t) <= _MAX_LEN):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= max_examples:
            break
    return out


def load_voice_examples() -> list[str]:
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        return [e for e in data.get("examples", []) if isinstance(e, str)]
    except Exception:
        return []
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `python -m pytest tests/test_voice_profile.py -v`
Expected: 6 passed

- [ ] **Step 5: Скрипт сборки `scripts/build_voice_profile.py`**

```python
"""Собирает профиль голоса оператора из стянутой с VPS базы.

Usage:
    bash scripts/pull_kb.sh
    python scripts/build_voice_profile.py
    # → bot/prompts/voice_profile.json (закоммитить и задеплоить)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.voice_profile import extract_voice_examples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/hde_bot_vps.db")
    ap.add_argument("--out", default="bot/prompts/voice_profile.json")
    ap.add_argument("--max", type=int, default=8)
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: база не найдена: {args.db}. Сначала bash scripts/pull_kb.sh", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    rows = [
        r[0]
        for r in conn.execute(
            # Только 'corrected': этот текст оператор набирал сам.
            # 'sent' может быть ИИ-ответом, отправленным как есть, — в эталон
            # голоса его брать нельзя.
            "SELECT op_answer FROM optimization_samples "
            "WHERE op_answer IS NOT NULL AND op_answer != '' "
            "AND outcome = 'corrected' "
            "ORDER BY created_at DESC"
        )
    ]
    examples = extract_voice_examples(rows, max_examples=args.max)
    if not examples:
        print("Нет подходящих op_answer — профиль не создан", file=sys.stderr)
        return 1

    Path(args.out).write_text(
        json.dumps({"examples": examples}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Сохранено {len(examples)} примеров в {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Прогнать скрипт на стянутой базе (если Task 1 удался)**

Run: `python scripts/build_voice_profile.py`
Expected: `Сохранено N примеров в bot/prompts/voice_profile.json` (или честная ошибка про отсутствие базы — тогда пропустить, файл создастся позже). Просмотреть содержимое файла глазами: тексты должны быть реальными ответами клиентам, без внутренних заметок.

- [ ] **Step 7: Commit**

```bash
git add bot/voice_profile.py scripts/build_voice_profile.py tests/test_voice_profile.py
git add bot/prompts/voice_profile.json 2>/dev/null || true
git commit -m "feat(style): voice profile module and builder script"
```

---

### Task 7: Подмешивание профиля голоса в системный промт

**Files:**
- Modify: `bot/ai_summary.py:37` (рядом с `_FEW_SHOT_EXAMPLES`) и `bot/ai_summary.py:257-270` (внутри `_build_system_prompt`)
- Test: `tests/test_voice_profile.py` (дописать)

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_voice_profile.py`:

```python
def test_build_system_prompt_includes_voice_examples(monkeypatch):
    import bot.ai_summary as ai

    monkeypatch.setattr(ai, "_VOICE_EXAMPLES", ["Проверьте чековую ленту и перезапустите кассу"])
    prompt = ai._build_system_prompt("Не печатает чек")
    assert "Проверьте чековую ленту" in prompt
    assert "в этом стиле" in prompt


def test_build_system_prompt_no_voice_block_when_empty(monkeypatch):
    import bot.ai_summary as ai

    monkeypatch.setattr(ai, "_VOICE_EXAMPLES", [])
    prompt = ai._build_system_prompt("Не печатает чек")
    assert "в этом стиле" not in prompt
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python -m pytest tests/test_voice_profile.py -v -k system_prompt`
Expected: FAIL с `AttributeError: ... has no attribute '_VOICE_EXAMPLES'`

- [ ] **Step 3: Реализация**

В `bot/ai_summary.py` после строки `_FEW_SHOT_EXAMPLES: list[dict] = _load_few_shot_examples()` (строка 37) добавить:

```python
from .voice_profile import load_voice_examples

_VOICE_EXAMPLES: list[str] = load_voice_examples()
```

(импорт поднять к остальным импортам модуля, если стиль файла того требует)

В `_build_system_prompt`, сразу ПОСЛЕ few-shot блока (после строки 270, перед `if rag_examples:`), добавить:

```python
    if _VOICE_EXAMPLES:
        base += (
            "Реальные ответы оператора клиентам. Пиши поле «Клиенту» в этом стиле —\n"
            "та же длина, тот же тон, без шаблонной вежливости:\n\n"
            + "\n".join(f"— {ex}" for ex in _VOICE_EXAMPLES)
            + "\n\n---\n\n"
        )
```

- [ ] **Step 4: Запустить тесты ai_summary + новые**

Run: `python -m pytest tests/test_voice_profile.py tests/test_ai_answer_quality.py tests/test_ai_phase2a.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add bot/ai_summary.py tests/test_voice_profile.py
git commit -m "feat(style): inject operator voice examples into summary system prompt"
```

---

### Task 8: LLMRouter — Gemini-first для мутаций

**Files:**
- Modify: `bot/optimizer/llm_router.py:143-186` (`complete_all`)
- Test: `tests/test_prompt_optimizer.py` (найти существующие тесты роутера и обновить/дописать)

- [ ] **Step 1: Посмотреть существующие тесты роутера**

Run: `python -m pytest tests/test_prompt_optimizer.py tests/test_prompt_optimization.py -v --collect-only -q`
Найти тесты с `router`/`complete_all` в имени и прочитать их код — они могут зашивать Groq-first порядок.

- [ ] **Step 2: Написать падающий тест**

Дописать (в тот файл, где уже живут тесты роутера; если их нет — в `tests/test_prompt_optimizer.py`):

```python
import pytest

from bot.optimizer.llm_router import LLMRouter


class _FakeClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.called = False

    async def complete(self, system, user):
        self.called = True
        if self.error:
            raise RuntimeError(self.error)
        return self.text


@pytest.mark.asyncio
async def test_complete_all_gemini_first_skips_groq_on_success():
    router = LLMRouter(gemini_api_key="k", groq_api_key="k")
    gemini = _FakeClient(text="мутация от gemini")
    llama = _FakeClient(text="мутация от llama")
    router.clients = {"gemini": gemini, "llama": llama}

    results, errors = await router.complete_all("s", "u")
    assert results == {"gemini": "мутация от gemini"}
    assert llama.called is False


@pytest.mark.asyncio
async def test_complete_all_falls_back_to_groq_when_gemini_fails():
    router = LLMRouter(gemini_api_key="k", groq_api_key="k")
    gemini = _FakeClient(error="quota")
    llama = _FakeClient(text="мутация от llama")
    router.clients = {"gemini": gemini, "llama": llama}

    results, errors = await router.complete_all("s", "u")
    assert results == {"llama": "мутация от llama"}
    assert "gemini" in errors
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `python -m pytest tests/test_prompt_optimizer.py -v -k gemini_first`
Expected: FAIL (текущий код зовёт Groq первым: `llama.called is True` / gemini не в results)

- [ ] **Step 4: Реализация — инвертировать приоритет в `complete_all`**

Заменить тело после определения `_safe_complete` (строки 163-186):

```python
        groq_clients = {k: v for k, v in self.clients.items() if k != "gemini"}
        gemini_client = self.clients.get("gemini")

        results: dict[str, str] = {}
        errors: dict[str, str] = {}

        # Gemini first — мутации самой умной модели. Groq остаётся резервом.
        if gemini_client is not None:
            name, text, err = await _safe_complete("gemini", gemini_client)
            if text is not None:
                results["gemini"] = text
            elif err is not None:
                errors["gemini"] = err

        # Fall back to Groq models (in parallel) only if Gemini failed
        if not results:
            groq_tasks = [_safe_complete(name, client) for name, client in groq_clients.items()]
            groq_results = await asyncio.gather(*groq_tasks)
            for name, text, err in groq_results:
                if text is not None:
                    results[name] = text
                elif err is not None:
                    errors[name] = err

        return results, errors
```

Обновить docstring `complete_all`: `"""Try Gemini first; fall back to Groq clients (parallel) if it fails. ..."""`. Если существующие тесты зашивали Groq-first — обновить их ожидания (это осознанное изменение поведения).

- [ ] **Step 5: Запустить все тесты оптимизатора**

Run: `python -m pytest tests/test_prompt_optimizer.py tests/test_prompt_optimization.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add bot/optimizer/llm_router.py tests/test_prompt_optimizer.py
git commit -m "feat(optimizer): mutations via Gemini first, Groq as fallback"
```

---

### Task 9: Holdout-гейт в ночном оптимизаторе

**Files:**
- Modify: `bot/optimizer/agent.py:62-198` (`run_optimizer`)
- Test: `tests/test_prompt_optimizer.py` (дописать)

- [ ] **Step 1: Написать падающий тест**

Найти в `tests/test_prompt_optimizer.py` существующие тесты `run_optimizer` (как мокается bot/db/router) и по их образцу добавить:

```python
@pytest.mark.asyncio
async def test_run_optimizer_applies_by_holdout_score(monkeypatch):
    """Победитель определяется holdout_score, а не train-скором."""
    from bot.optimizer import agent

    samples = [
        {"id": i, "ticket_id": str(i), "title": "t", "history": f"h{i}",
         "ai_answer": "a", "op_answer": "Клиенту: ответ", "outcome": "corrected"}
        for i in range(20)
    ]

    async def fake_get_samples(days=30):
        return samples

    async def fake_combined(s, instructions, **kw):
        return 0.5  # train-скор одинаковый у всех

    holdout_calls = []

    async def fake_holdout(s, instructions, **kw):
        holdout_calls.append(instructions)
        # текущая инструкция хуже мутации на holdout
        return 0.4 if instructions == "CURRENT" else 0.9

    async def fake_get_active():
        return "CURRENT"

    class FakeRouter:
        def __init__(self, *a, **kw): pass
        async def complete_all(self, system, user):
            return {"gemini": "MUTATED INSTRUCTIONS LONG ENOUGH TO PASS"}, {}

    saved = {}

    async def fake_save_version(content, score, proposed_by):
        saved["content"] = content
        return 7

    monkeypatch.setattr(agent.db, "get_optimization_samples", fake_get_samples)
    monkeypatch.setattr(agent.db, "save_prompt_version", fake_save_version)
    monkeypatch.setattr(agent, "combined_score", fake_combined)
    monkeypatch.setattr(agent, "holdout_score", fake_holdout)
    monkeypatch.setattr(agent, "get_active_format_instructions", fake_get_active)
    monkeypatch.setattr(agent, "LLMRouter", FakeRouter)

    sent = []

    class FakeBot:
        async def send_message(self, **kw):
            sent.append(kw)

            class _M:
                async def edit_text(self, *a, **k): pass
            return _M()

    await agent.run_optimizer(FakeBot())
    # holdout_score вызван и для CURRENT (baseline), и для мутации
    assert "CURRENT" in holdout_calls
    assert any("MUTATED" in c for c in holdout_calls)
    # отчёт с кнопкой apply отправлен (0.9 > 0.4 + 0.03)
    assert any("opt:apply" in str(kw.get("reply_markup", "")) for kw in sent)
```

(точную форму моков подогнать под существующий стиль файла — там уже есть тесты `run_optimizer`)

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python -m pytest tests/test_prompt_optimizer.py -v -k holdout`
Expected: FAIL с `AttributeError: module 'bot.optimizer.agent' has no attribute 'holdout_score'`

- [ ] **Step 3: Реализация в `agent.py`**

Импорты (после существующих):

```python
from .dataset import split_samples
from .judge import holdout_score
```

После загрузки сэмплов (строка 62, после проверки `_MIN_SAMPLES`) добавить split:

```python
    train, holdout = split_samples(samples)
    if not holdout or not train:
        # слишком мало данных для честного сплита — оцениваем на всём
        train, holdout = samples, samples
```

Заменить использования:
- `baseline = await combined_score(samples, current_instructions)` → `baseline = await combined_score(train, current_instructions)`
- `good = [s for s in samples ...]` / `bad = [...]` → строить из `train`
- скоринг мутаций `score = await combined_score(samples, content)` → `score = await combined_score(train, content)`

После цикла скоринга мутаций и проверки `if not scores:` ЗАМЕНИТЬ блок выбора победителя (строки 156-157) и гейт (строка 164) на holdout-гейт:

```python
    if progress_msg:
        await _update_progress(progress_msg, 78, "Финальная проверка на holdout-наборе...")

    # Holdout-гейт: топ-2 кандидата по train-скору пересчитываются судьёй
    # на отложенных сэмплах; apply-решение — только по holdout.
    top_models = sorted(scores, key=lambda m: scores[m][1], reverse=True)[:2]
    try:
        baseline_holdout = await holdout_score(holdout, current_instructions)
        final_scores: dict[str, float] = {}
        for model_name in top_models:
            final_scores[model_name] = await holdout_score(holdout, scores[model_name][0])
    except Exception as exc:
        logger.warning("Optimizer: holdout evaluation failed: %s", exc)
        if progress_msg:
            await _update_progress(progress_msg, 78, f"❌ Ошибка holdout-оценки: {exc}")
        return

    winner_model = max(final_scores, key=lambda m: final_scores[m])
    winner_content = scores[winner_model][0]
    winner_score = final_scores[winner_model]
    baseline = baseline_holdout  # отчёт и гейт ниже сравнивают holdout с holdout
```

Гейт `if winner_score < baseline + _MIN_IMPROVEMENT:` остаётся как есть (теперь обе величины — holdout). В `_send_report` ничего менять не нужно (передаются уже holdout-числа).

- [ ] **Step 4: Запустить тесты оптимизатора**

Run: `python -m pytest tests/test_prompt_optimizer.py tests/test_prompt_optimization.py -v`
Expected: all passed. Если старые тесты `run_optimizer` падают из-за нового вызова `holdout_score` — замокать его в них так же, как `combined_score`.

- [ ] **Step 5: Commit**

```bash
git add bot/optimizer/agent.py tests/test_prompt_optimizer.py
git commit -m "feat(optimizer): holdout gate with LLM-judge for nightly apply decision"
```

---

### Task 10: Eval-харнесс CLI

**Files:**
- Create: `scripts/eval_prompt.py`
- Test: `tests/test_eval_harness.py`

- [ ] **Step 1: Написать падающие тесты (чистые части)**

```python
"""Tests for eval harness pure parts (no network)."""
import sqlite3

from scripts.eval_prompt import EvalCache, _prompt_hash, load_samples


def test_prompt_hash_stable_and_distinct():
    assert _prompt_hash("a") == _prompt_hash("a")
    assert _prompt_hash("a") != _prompt_hash("b")


def test_eval_cache_roundtrip(tmp_path):
    cache = EvalCache(str(tmp_path / "c.db"))
    assert cache.get("k1") is None
    cache.put("k1", "ответ")
    assert cache.get("k1") == "ответ"
    # повторное открытие видит данные
    cache2 = EvalCache(str(tmp_path / "c.db"))
    assert cache2.get("k1") == "ответ"


def test_load_samples(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE optimization_samples ("
        "id INTEGER PRIMARY KEY, ticket_id TEXT, title TEXT, history TEXT, "
        "ai_answer TEXT, op_answer TEXT, outcome TEXT, confidence INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO optimization_samples "
        "(ticket_id, title, history, ai_answer, op_answer, outcome) "
        "VALUES ('1', 't', 'h', 'a', 'op', 'corrected')"
    )
    conn.commit()
    rows = load_samples(str(db), days=365)
    assert len(rows) == 1
    assert rows[0]["op_answer"] == "op"
```

Если `scripts` не импортируется как пакет — в начале теста добавить тот же `sys.path` хак, что в самом скрипте, либо создать пустой `scripts/__init__.py` (проверить, как тестируются существующие скрипты; `scripts/__pycache__` существует — значит импорты оттуда уже работают).

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `python -m pytest tests/test_eval_harness.py -v`
Expected: FAIL с ImportError/ModuleNotFoundError

- [ ] **Step 3: Реализация `scripts/eval_prompt.py`**

```python
"""Офлайн eval-харнесс: сравнение промт-кандидата с активным на реальных данных.

Usage:
    bash scripts/pull_kb.sh                                  # стянуть свежую базу
    python scripts/eval_prompt.py --prompt candidate.md      # файл с кандидатом
    python scripts/eval_prompt.py --version 12               # версия из prompt_versions
    python scripts/eval_prompt.py                            # active vs последний candidate
    python scripts/eval_prompt.py --no-judge                 # быстрый прогон без судьи

Требуется GEMINI_API_KEY в окружении. Ответы кешируются в data/eval_cache.db —
прерванный прогон продолжается с того же места, повторные прогоны бесплатны.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.optimizer import evaluator
from bot.optimizer.dataset import split_samples
from bot.optimizer.judge import holdout_score

MIN_SAMPLES_WARN = 30


def _prompt_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class EvalCache:
    """Кеш сгенерированных ответов: (prompt, history, title) → answer."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_cache (key TEXT PRIMARY KEY, answer TEXT)"
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT answer FROM eval_cache WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def put(self, key: str, answer: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO eval_cache (key, answer) VALUES (?,?)", (key, answer)
        )
        self.conn.commit()


def make_cached_generate(cache: EvalCache, prompt_text: str):
    ph = _prompt_hash(prompt_text)

    async def generate(history: str, title: str, instructions: str) -> str:
        key = hashlib.sha1(f"{ph}|{title}|{history}".encode("utf-8")).hexdigest()
        hit = cache.get(key)
        if hit is not None:
            return hit
        # Простой backoff на 429/сетевые ошибки; после 3 неудач — пробрасываем,
        # combined_score пропустит сэмпл, а кеш позволит дорешать повторным запуском.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                answer = await evaluator._generate_answer(history, title, instructions)
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(10 * (attempt + 1))
        else:
            raise last_exc  # type: ignore[misc]
        cache.put(key, answer)
        return answer

    return generate


def load_samples(db_path: str, days: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, confidence "
        "FROM optimization_samples WHERE created_at >= datetime('now', ?) ORDER BY id",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def load_prompt_from_db(db_path: str, version: int | None) -> tuple[str, str]:
    """Returns (label, content). version=None → последний candidate."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if version is not None:
        row = conn.execute(
            "SELECT id, content FROM prompt_versions WHERE id=?", (version,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, content FROM prompt_versions WHERE status='candidate' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("Кандидат не найден в prompt_versions")
    return f"v{row['id']}", row["content"]


def load_active_prompt(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT content FROM prompt_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SystemExit(
            "В prompt_versions нет активной версии — применить текущий промт "
            "или передать --prompt для обоих кандидатов"
        )
    return row[0]


async def evaluate_prompt(
    label: str,
    prompt_text: str,
    train: list[dict],
    holdout: list[dict],
    cache: EvalCache,
    use_judge: bool,
) -> dict:
    generate = make_cached_generate(cache, prompt_text)
    train_score = await evaluator.combined_score(
        train, prompt_text, max_samples=None, _generate_fn=generate
    )
    if use_judge:
        hold = await holdout_score(holdout, prompt_text, _generate_fn=generate)
    else:
        hold = await evaluator.combined_score(
            holdout, prompt_text, max_samples=None, _generate_fn=generate
        )
    return {"label": label, "train": train_score, "holdout": hold}


async def worst_regressions(
    holdout: list[dict],
    active_text: str,
    cand_text: str,
    cache: EvalCache,
    top_n: int = 3,
) -> list[tuple[float, dict, str]]:
    gen_active = make_cached_generate(cache, active_text)
    gen_cand = make_cached_generate(cache, cand_text)
    diffs: list[tuple[float, dict, str]] = []
    for s in holdout:
        ref = s.get("op_answer") or s.get("ai_answer") or ""
        if not ref:
            continue
        a = await gen_active(s["history"], s["title"], active_text)
        c = await gen_cand(s["history"], s["title"], cand_text)
        ra = difflib.SequenceMatcher(None, a.lower(), ref.lower()).ratio()
        rc = difflib.SequenceMatcher(None, c.lower(), ref.lower()).ratio()
        diffs.append((rc - ra, s, c))
    diffs.sort(key=lambda x: x[0])
    return [d for d in diffs if d[0] < 0][:top_n]


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/hde_bot_vps.db")
    ap.add_argument("--prompt", help="файл с промтом-кандидатом")
    ap.add_argument("--version", type=int, help="id версии из prompt_versions")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--cache", default="data/eval_cache.db")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: база не найдена: {args.db}. Сначала bash scripts/pull_kb.sh", file=sys.stderr)
        return 1

    samples = load_samples(args.db, args.days)
    if len(samples) < MIN_SAMPLES_WARN:
        print(
            f"⚠️  Всего {len(samples)} сэмплов (< {MIN_SAMPLES_WARN}) — "
            "оценка ненадёжна, относись к цифрам скептически."
        )
    if not samples:
        print("ERROR: optimization_samples пуст", file=sys.stderr)
        return 1

    train, holdout = split_samples(samples)
    if not holdout or not train:
        train = holdout = samples
    print(f"Сэмплов: {len(samples)} (train {len(train)} / holdout {len(holdout)})")

    active_text = load_active_prompt(args.db)
    if args.prompt:
        cand_label, cand_text = Path(args.prompt).name, Path(args.prompt).read_text(encoding="utf-8")
    else:
        cand_label, cand_text = load_prompt_from_db(args.db, args.version)

    cache = EvalCache(args.cache)
    use_judge = not args.no_judge

    results = []
    for label, text in (("active", active_text), (cand_label, cand_text)):
        print(f"Оцениваю «{label}»...")
        results.append(await evaluate_prompt(label, text, train, holdout, cache, use_judge))

    print("\n=== Результаты ===")
    print(f"{'промт':<20} {'train':>8} {'holdout':>8}")
    for r in results:
        print(f"{r['label']:<20} {r['train']:>8.3f} {r['holdout']:>8.3f}")
    delta = results[1]["holdout"] - results[0]["holdout"]
    verdict = "✅ кандидат лучше" if delta > 0 else "❌ кандидат не лучше"
    print(f"\nДельта на holdout: {delta:+.3f} — {verdict}")

    regs = await worst_regressions(holdout, active_text, cand_text, cache)
    if regs:
        print("\n=== Худшие регрессии (кандидат хуже active) ===")
        for diff, s, cand_answer in regs:
            print(f"\n[{diff:+.3f}] тикет {s['ticket_id']}: {s.get('title', '')[:60]}")
            print(f"  Оператор: {(s.get('op_answer') or '')[:150]}")
            print(f"  Кандидат: {cand_answer[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
```

- [ ] **Step 4: Запустить тесты**

Run: `python -m pytest tests/test_eval_harness.py -v`
Expected: 3 passed

- [ ] **Step 5: Интеграционный прогон вручную (если есть база и ключ)**

Run: `GEMINI_API_KEY=<ключ из secrets/.env> python scripts/eval_prompt.py --no-judge --days 365`
Expected: таблица скоров без ошибок. Если базы нет — пропустить, отметить в итоговом отчёте.

- [ ] **Step 6: Commit**

```bash
git add scripts/eval_prompt.py tests/test_eval_harness.py
git commit -m "feat(eval): offline prompt eval harness with cache and judge scoring"
```

---

### Task 11: Скрипт заливки промта на VPS

**Files:**
- Create: `scripts/push_prompt.py`

- [ ] **Step 1: Написать скрипт (запускается НА VPS, чистый stdlib)**

```python
"""Залить промт-кандидат в prompt_versions. Запускается на VPS.

Usage (на VPS, из /opt/hde-bot):
    python3 scripts/push_prompt.py candidate.md            # добавить как candidate
    python3 scripts/push_prompt.py candidate.md --apply    # сразу применить

После --apply нужен рестарт бота (кеш промта в памяти):
    sudo systemctl restart hde-bot

Локальный цикл целиком:
    python scripts/eval_prompt.py --prompt candidate.md    # убедиться, что лучше
    scp candidate.md config1:/opt/hde-bot/
    ssh config1 "cd /opt/hde-bot && python3 scripts/push_prompt.py candidate.md --apply && sudo systemctl restart hde-bot"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file")
    ap.add_argument("--db", default="hde_bot.db")
    ap.add_argument("--apply", action="store_true", help="сразу сделать активной")
    args = ap.parse_args()

    text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if len(text) < 50:
        print("ERROR: промт подозрительно короткий, отмена", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    cur = conn.execute(
        "INSERT INTO prompt_versions (content, status, proposed_by) "
        "VALUES (?, 'candidate', 'manual')",
        (text,),
    )
    vid = cur.lastrowid
    if args.apply:
        conn.execute(
            "UPDATE prompt_versions SET status='rejected' "
            "WHERE status IN ('active', 'candidate') AND id != ?",
            (vid,),
        )
        conn.execute(
            "UPDATE prompt_versions SET status='active', applied_at=datetime('now') "
            "WHERE id=?",
            (vid,),
        )
    conn.commit()
    state = "применена (нужен restart бота)" if args.apply else "сохранена как candidate"
    print(f"Версия {vid} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Проверить локально на временной базе**

Run:
```bash
python - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/test_push.db")
conn.execute("CREATE TABLE prompt_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, score REAL, proposed_by TEXT, status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL DEFAULT (datetime('now')), applied_at TEXT)")
conn.commit()
EOF
printf 'Это тестовый промт достаточной длины, чтобы пройти проверку минимального размера текста.' > data/test_prompt.md
python scripts/push_prompt.py data/test_prompt.md --db data/test_push.db --apply
```
Expected: `Версия 1 применена (нужен restart бота)`

- [ ] **Step 3: Commit**

```bash
git add scripts/push_prompt.py
git commit -m "feat(eval): push_prompt.py to deploy prompt candidates to VPS"
```

---

### Task 12: Полный прогон и финал

- [ ] **Step 1: Полный тест-сьют**

Run: `python -m pytest tests/ -q`
Expected: все проходят, кроме 3 преданно падающих (pre-existing failures, зафиксированы ранее — test_hde_payload и связанные; новых падений быть не должно).

- [ ] **Step 2: Если Task 1 удался — собрать профиль голоса и прогнать харнесс целиком**

```bash
bash scripts/pull_kb.sh
python scripts/build_voice_profile.py
python scripts/eval_prompt.py --no-judge --days 365
```
Expected: профиль создан, харнесс печатает скоры active.

- [ ] **Step 3: Закоммитить voice_profile.json, если создан**

```bash
git add bot/prompts/voice_profile.json
git commit -m "feat(style): initial operator voice profile from production data"
```

- [ ] **Step 4: Деплой НЕ делать без явного запроса пользователя**

Деплой-цикл (когда попросят): `git push`, затем `ssh config1 "cd /opt/hde-bot && git pull && sudo systemctl restart hde-bot"`.

# Ночная сверка «предложил бот ↔ ответил человек» (автообучение без кнопок)

**Дата:** 2026-07-13
**Проблема:** операторы не жмут кнопки бота и не отвечают через него → петля обучения
мёртвая (см. память optimization-samples-stale). Нужен сбор сигнала БЕЗ участия
операторов: сверять ночью предложенный ботом ответ с фактическим ответом оператора
в тикете.

**Режим (решено с юзером 2026-07-13):** сбор + утренняя сводка. Обновление образцов
стиля — только с ручного «ок», не авто.

## Что уже есть (переиспользуем)
- Бот сохраняет свой ответ: `ai_suggestions` (ai_answer, ticket_id, context_until_post_id).
- Поля под сверку заложены: `judge_reference_answer`, `judge_label`, `judge_detail`, `judged_at`.
- Судья: `bot/optimizer/judge.py:judge_answer` (LLM-сравнение answer↔reference).
- Дешёвое сравнение по тексту: `bot/optimizer/evaluator.py` (difflib similarity).
- Роль-атрибуция реплик: `bot/agent/dialogue_mining.split_ticket_into_pairs`.
- Ночной слот в планировщике (`bot/scheduler.py`, рядом с nightly dialogue backfill).
- Утренняя сводка: `bot/digest.py` + `format_morning_digest`.
- Троттл HDE API (80/min) — ночной сбор безопасен.
- Voice-билдер из corrected: `scripts/build_voice_profile.py` (уже перецелен).

## Что дособрать
1. **Reconciler** (новый модуль, напр. `bot/agent/reconcile.py`):
   для каждого «свежего» ai_suggestions без `judged_at`:
   - вытащить фактический ответ оператора после `context_until_post_id`
     (первый staff-turn после якоря; переиспользовать split_ticket_into_pairs
     + staff-set из `AGENT_STAFF_USER_IDS`);
   - если ответа нет (тикет брошен/ещё в работе) → пропустить, не судить;
   - сохранить фактический ответ в `judge_reference_answer`;
   - метка: дешёвый similarity (difflib) → «matched» (≥порог) / «diverged» (<порог);
     при желании усилить LLM-судьёй `judge_answer` (Groq, на проде, ночью).
   - записать `judge_label`, `judge_detail` (score), `judged_at`; пересчитать effective_label.
2. **Ночной запуск**: хук в scheduler рядом с dialogue backfill (раз в сутки, после смены,
   MSK-время; троттл покрывает API).
3. **Раздел в утренней сводке** (`digest.py`/`formatter.py`): за прошедший день —
   сколько matched / diverged / без ответа + топ-5 diverged («бот: X | человек: Y»).
4. **Обновление стиля — отдельная ручная команда** (не в авто): по diverged-эталонам
   пересобрать voice_profile строгим фильтром (как 2026-07-13), показать дифф, ждать «ок».

## Решения (locked / открытые)
- LOCKED: сбор+сводка, стиль — только с «ок».
- ОТКРЫТО: порог similarity matched/diverged (старт ~0.6, подбор по факту).
- ОТКРЫТО: судья — только difflib (дёшево, без Groq) ИЛИ + LLM-судья (точнее, ест TPM).
  Рекоменд.: старт difflib; LLM-судья позже, если difflib шумит.
- ОТКРЫТО: горизонт сверки (тикеты за последние N дней; старт 1 день/ночь).

## Проверка (goal-driven)
- Юнит: reconciler на фикстуре (есть ответ оператора → matched/diverged; нет → skip; роли не путаются).
- Юнит: секция сводки рендерит счётчики + топ-diverged.
- Интеграция: один ночной прогон на проде → в `ai_suggestions` появились judge_label/reference; утром пришла сводка.
- Без бана: API только под троттлом, ночью, один проход.

## НЕ делаем сейчас
- Автообновление стиля/промпта без ручного «ок».
- Автоправку промпта (только сигнал + сводка; тюнинг промпта — отдельно, путь D).

# /prompt-cycle — цикл улучшения промта суммарок через Claude (без Gemini)

Выполни полный цикл улучшения промта «Суть / Клиенту / Памятка» на свежих боевых данных.
Все интеллектуальные шаги (анализ, правка, генерация, судейство) делаются Claude-агентами,
Gemini не используется. Работай автономно, останавливайся только на финальном вопросе о деплое.

## Контекст

- Активный промт живёт в таблице `prompt_versions` (status='active') в SQLite на VPS
  (хост `config1`, путь `/opt/hde-bot/hde_bot.db`). Бот читает его через
  `get_active_format_instructions()`; кеш в памяти — после смены нужен restart.
- Датасет: таблица `optimization_samples` (id, ticket_id, title, history, ai_answer,
  op_answer, outcome: sent|accepted|corrected|rejected). op_answer при outcome='corrected' —
  текст, который оператор написал сам (эталон голоса).
- Готовые скрипты: `scripts/pull_kb.sh` (стянуть базу), `scripts/eval_prompt.py`
  (офлайн-скоринг с кешем), `scripts/fill_eval_cache.py` (залить ответы агентов в кеш),
  `scripts/build_voice_profile.py` (профиль голоса), `scripts/push_prompt.py` (деплой, на VPS).
- Для импорта bot.* нужны env-заглушки: `BOT_TOKEN=dummy PERSONAL_CHAT_ID=1 GROUP_CHAT_ID=1 WEBHOOK_HOST=https://dummy GEMINI_API_KEY=dummy`.
- Стайлгайд: `bot/prompts/style_guide_ru.md`. История подхода: память
  `prompt-optimization-state`, спека `docs/superpowers/specs/2026-06-11-offline-eval-harness-design.md`.

## Шаги

### 1. Свежие данные
```bash
bash scripts/pull_kb.sh
```

### 2. Текущий промт и сэмплы
Вытащи из `data/hde_bot_vps.db`:
- активную версию → `artifacts/current_prompt.md` (запомни её id и `applied_at`);
- все сэмплы → `artifacts/eval_samples.json` (массив `{id, title, history_tail}`,
  где history_tail = последние 2000 символов history — ровно как в
  `evaluator._generate_answer`) и `artifacts/eval_refs.json`
  (`{id, outcome, op_answer, ai_answer[:200]}`).

### 3. Анализ новых провалов
Новые сэмплы = `created_at > applied_at` активной версии. Выведи их rejected/corrected
(ai_answer vs op_answer), найди паттерны провалов. Если новых провалов нет — скажи об
этом и спроси, продолжать ли на всех данных.

### 4. Кандидат
Скопируй `artifacts/current_prompt.md` → `artifacts/candidate_prompt.md` и внеси
ХИРУРГИЧЕСКИЕ правки под найденные паттерны (структуру reasoning + три секции не ломай).
Принципы из прошлых итераций: ничего не выдумывать (телефоны, ID, обещания, статусы);
живой язык вместо канцелярита (стайлгайд); первое лицо при координации («я подключаюсь»);
объяснение вместо дефолтного запроса удалённого доступа; «Суть» — заметка инженера коллеге;
«Памятка» — слоты, включая «что пробовали — результат» и «статус/договорённости»;
реальные провалы добавляй в плохие примеры, реальные ответы оператора — в хорошие.

Заодно: если corrected-сэмплов прибавилось — пересобери профиль голоса
(`python scripts/build_voice_profile.py`); если `bot/prompts/voice_profile.json`
изменился — закоммить и задеплой через git отдельно от промта.

### 5. Генерация (4 агента параллельно, фон)
Запусти 4 general-purpose агента (model sonnet): current×2 и candidate×2
(сэмплы 1–15 и 16–30 по порядку файла). Промт агента — дословно:

> Ты симулируешь LLM-ассистента техподдержки кассового оборудования для офлайн-экзамена
> промта. Работа из d:\HDE_bot.
> 1. Прочитай файл `artifacts/<current|candidate>_prompt.md` — это системная инструкция,
>    которую ты обязан исполнять БУКВАЛЬНО (не улучшай её; если она чего-то не оговаривает —
>    действуй как обычная LLM).
> 2. Прочитай `artifacts/eval_samples.json`. Возьми <первые 15 | с 16-го по 30-й> сэмплов.
> 3. Для каждого: системный промт = "Ты AI-ассистент специалиста 2-й линии поддержки
>    кассового оборудования.\n\n" + инструкция; ввод = "Тема тикета: {title}\n\n{history_tail}".
>    Ответ строго в формате трёх меток (Суть: / Клиенту: / Памятка:), БЕЗ блока <reasoning>,
>    без markdown.
> 4. Запиши в `artifacts/gen_<current|candidate>_<1|2>.json` формата {"<id>": "<ответ>"},
>    utf-8, ensure_ascii=false.
> Отвечай как добросовестная модель уровня Gemini Flash, строго следующая инструкции,
> ТОЛЬКО по информации из history_tail. Ничего не выдумывай.

### 6. Механический скор (опционально, если хочется цифру харнесса)
```bash
python -X utf8 scripts/fill_eval_cache.py --prompt artifacts/current_prompt.md --answers artifacts/gen_current_1.json artifacts/gen_current_2.json
python -X utf8 scripts/fill_eval_cache.py --prompt artifacts/candidate_prompt.md --answers artifacts/gen_candidate_1.json artifacts/gen_candidate_2.json
BOT_TOKEN=dummy PERSONAL_CHAT_ID=1 GROUP_CHAT_ID=1 WEBHOOK_HOST=https://dummy GEMINI_API_KEY=dummy \
  python -X utf8 scripts/eval_prompt.py --prompt artifacts/candidate_prompt.md --no-judge --days 365
```
Помни: метрика сравнивает со старыми принятыми ответами — у текущего промта домашнее
преимущество; ничья для кандидата = хорошо. Главный вердикт — слепое судейство.

### 7. Слепое судейство
Собери пары: для каждого id возьми оба ответа; при нечётном id X=candidate, при чётном
X=current (детерминированное ослепление); ключ расшифровки сохрани в
`artifacts/judge_key.json`. Пары (по 15) → `artifacts/judge_pairs_1.json` и `_2.json`,
каждая запись: `{id, title, history: history_tail[-800:], outcome, op_answer, X, Y}`.

Запусти 2 агентов-судей (model opus) параллельно. Промт судьи: слепой судья качества
ответов поддержки кассового оборудования; критерии по убыванию веса:
1) выдуманные факты (телефоны, ID, ссылки, обещания, статусы не из переписки) = дисквалификация;
2) «Клиенту» звучит как живой оператор (без канцелярита и приветствий, первое лицо при
   координации, ≤20 слов, точная терминология: ID AnyDesk / ID и пароль RuDesktop);
3) непонимание клиента → короткое объяснение лучше запроса удалённого доступа;
4) «Суть» — живая заметка инженера коллеге (не «клиент обратился», не «причина не установлена»);
   «Памятка» — конкретика и слоты, прочерк при наличии конкретики в истории — минус;
5) при наличии op_answer — близость к реальному оператору решает.
Вердикт "X"|"Y"|"tie" → `artifacts/verdicts_1.json` / `_2.json` формата
`{"<id>": {"winner": ..., "reason": "<одна фраза>"}}`.

### 8. Расшифровка и отчёт
Расшифруй по judge_key, посчитай счёт, покажи: итог, 3–4 показательные пары
(current vs candidate side-by-side), причины проигрышей кандидата. Если кандидат
проиграл или ничья — доработай кандидата по причинам и повтори шаги 5–8 один раз;
если снова не лучше — честно скажи, что улучшений не найдено, и остановись.

### 9. Деплой (ТОЛЬКО после явного «да» пользователя)
```bash
scp artifacts/candidate_prompt.md config1:/opt/hde-bot/candidate.md
ssh config1 "cd /opt/hde-bot && python3 scripts/push_prompt.py candidate.md --apply && sudo systemctl restart hde-bot && systemctl is-active hde-bot"
```
Проверь в боевой базе, что новая версия active, и обнови память `prompt-optimization-state`
(номер версии, счёт экзамена, дата).

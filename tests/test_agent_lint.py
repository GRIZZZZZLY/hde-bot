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

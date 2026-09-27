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


def test_stress_ignores_words_that_merely_contain_markers():
    """Final review F8: «стоим» внутри «стоимость», «снова» внутри «основании»."""
    assert not cv2.detect_stress(["Какая стоимость подписки?"])
    assert not cv2.detect_stress(["Сделали чек на основании продажи"])
    assert not cv2.detect_stress(["В первую очередь интересует печать чеков"])


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


async def test_v2_history_has_a_hard_size_cap(monkeypatch):
    """Final review F2: одно огромное сообщение клиента не должно съесть 8000 TPM."""
    monkeypatch.setattr(config, "agent_voice_v2_enabled", True)
    monkeypatch.setattr(config, "agent_dynamic_fewshot_enabled", False)
    text = "НАЧАЛО " + "касса не печатает " * 500 + " КОНЕЦ"
    assert len(text) > 9000
    info = SimpleNamespace(client_id=1)

    async def no_embed(text, task_type="query"):
        return None

    async def none_async(*a, **k):
        return None

    ctx = await build_agent_context(
        [_p(1, 1, text)], info, "Не печатает", _embed_fn=no_embed,
        _similar_fn=none_async, _equipment_fn=lambda t, h: None, _wiki_fn=none_async,
        _pattern_fn=none_async, _topic_fn=none_async,
    )
    assert len(ctx["history"]) <= 3100
    assert "НАЧАЛО" in ctx["history"] and "КОНЕЦ" in ctx["history"]

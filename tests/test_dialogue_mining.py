import bot.config as config_module


def test_staff_ids_and_mining_flag(monkeypatch):
    monkeypatch.setenv("AGENT_STAFF_USER_IDS", "10, 20 ,30")
    monkeypatch.setenv("AGENT_DIALOGUE_MINING_ENABLED", "true")
    fresh = config_module.Config.from_env()
    assert fresh.agent_staff_user_ids == ("10", "20", "30")
    assert fresh.agent_dialogue_mining_enabled is True


def test_mining_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_DIALOGUE_MINING_ENABLED", raising=False)
    assert config_module.Config.from_env().agent_dialogue_mining_enabled is False


from types import SimpleNamespace

from bot.agent.dialogue_mining import (
    _strip_html,
    is_staff_post,
    sort_posts,
    split_ticket_into_pairs,
    staff_id_set,
)


def _post(pid, uid, text, is_comment=False, dc="00:00:00 01.01.2024"):
    return SimpleNamespace(post_id=pid, user_id=uid, text=text,
                           is_comment=is_comment, date_created=dc)


def test_strip_html():
    assert _strip_html("<p>не <b>печатает</b></p>") == "не печатает"


def test_sort_posts_orders_oldest_first():
    posts = [_post(3, "c", "c"), _post(1, "a", "a"), _post(2, "b", "b")]
    assert [p.post_id for p in sort_posts(posts)] == [1, 2, 3]


def test_staff_id_set_and_detection():
    assert staff_id_set("owner1", ("10", "20")) == {"10", "20"}
    assert staff_id_set("owner1", ()) == {"owner1"}
    assert is_staff_post(_post(1, "10", "x"), {"10"}) is True
    assert is_staff_post(_post(2, "99", "x"), {"10"}) is False


def test_split_merges_consecutive_operator_posts():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не работает"),
        _post(2, "op", "Добрый день."),
        _post(3, "op", "Уточните модель кассы."),
        _post(4, "op", "И пришлите фото ошибки."),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1                              # три staff-поста = один turn
    ans = pairs[0]["operator_answer"]
    assert "Добрый день." in ans and "модель кассы" in ans and "фото ошибки" in ans
    assert pairs[0]["operator_message_id"] == "2"       # первый пост turn'а
    assert pairs[0]["source_message_id"] == "1"         # последний клиентский пост


def test_split_multi_turn_no_future_leak():
    staff = {"op"}
    posts = [
        _post(1, "client", "касса не печатает"),
        _post(2, "op", "проверьте бумагу"),
        _post(3, "client", "бумага есть"),
        _post(4, "op", "перезагрузите кассу"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 2
    assert pairs[0]["operator_answer"] == "проверьте бумагу"
    assert "перезагрузите" not in pairs[0]["context"]   # нет утечки будущего
    assert "перезагрузите" not in pairs[0]["operator_answer"]
    assert "бумага есть" in pairs[1]["context"]
    assert pairs[0]["content_hash"] != pairs[1]["content_hash"]


def test_split_reversed_input_still_correct():
    staff = {"op"}
    posts = [
        _post(4, "op", "перезагрузите кассу"),
        _post(3, "client", "бумага есть"),
        _post(2, "op", "проверьте бумагу"),
        _post(1, "client", "касса не печатает"),
    ]  # обратный порядок на входе
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert pairs[0]["operator_answer"] == "проверьте бумагу"   # сортировка сработала
    assert "перезагрузите" not in pairs[0]["context"]


def test_split_skips_comments_and_leading_staff():
    staff = {"op"}
    posts = [
        _post(1, "op", "внутренняя заметка", is_comment=True),
        _post(2, "op", "ответ без клиента"),
        _post(3, "client", "вопрос"),
        _post(4, "op", "ответ"),
    ]
    pairs = split_ticket_into_pairs("T1", posts, staff)
    assert len(pairs) == 1
    assert pairs[0]["operator_answer"] == "ответ"


def test_content_hash_changes_with_edited_answer():
    staff = {"op"}
    base = [_post(1, "client", "вопрос"), _post(2, "op", "ответ")]
    edited = [_post(1, "client", "вопрос"), _post(2, "op", "ответ исправлен")]
    h1 = split_ticket_into_pairs("T1", base, staff)[0]["content_hash"]
    h2 = split_ticket_into_pairs("T1", edited, staff)[0]["content_hash"]
    assert h1 != h2                                     # правка ответа → новый хэш

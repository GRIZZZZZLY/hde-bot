# tests/test_agent_context.py
from types import SimpleNamespace

from bot.agent.context import build_agent_context, build_history_budgeted


def _mk_posts():
    return [
        SimpleNamespace(user_id=1, text="касса не печатает чек", post_id=1),
        SimpleNamespace(user_id=99, text="проверьте бумагу", post_id=2),
        SimpleNamespace(user_id=1, text="бумага есть", post_id=3),
    ]


def test_history_budgeted_keeps_whole_messages():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    def hist(p, i):
        return "\n".join(f"Клиент: {x.text}" if x.user_id == 1 else f"Сотрудник: {x.text}"
                         for x in p)

    full = build_history_budgeted(posts, info, budget=10_000, _history_fn=hist)
    assert "касса не печатает" in full and "бумага есть" in full

    # маленький бюджет: первый вопрос + маркер пропуска + хвост целыми сообщениями
    # (90 не форсирует обрезку: полный текст фикстуры — 77 символов, что уже
    # укладывается в 90; 60 меньше полного текста и оставляет ровно последнее
    # сообщение в хвосте)
    small = build_history_budgeted(posts, info, budget=60, _history_fn=hist)
    assert "касса не печатает" in small            # исходный вопрос сохранён
    assert "пропущено" in small
    assert "бумага есть" in small                  # последнее сообщение целиком


async def test_build_agent_context_evidence_structure():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def fake_similar(emb, *, limit=3, query_text="", company_id=""):
        item = SimpleNamespace(id=12, content="Меняли бумагу — помогла перезагрузка", quality="good")
        return [(item, 0.91)]

    async def fake_wiki(title):
        return "Статья про чеки"

    async def fake_pattern(equipment, keywords):
        return {"id": 3, "steps": "1. Проверить бумагу"}

    ctx = await build_agent_context(
        posts, info, "Не печатает чек", company_id="c1",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=fake_similar,
        _equipment_fn=lambda t, h: "АТОЛ", _wiki_fn=fake_wiki, _pattern_fn=fake_pattern,
    )
    kb = [e for e in ctx["evidence"] if e["source_type"] == "knowledge_item"][0]
    assert kb["source_id"] == 12 and kb["score"] == 0.91
    assert "перезагрузка" in kb["used_excerpt"]           # контент, не метка
    wiki = [e for e in ctx["evidence"] if e["source_type"] == "wiki"][0]
    assert "Статья" in wiki["used_excerpt"]
    assert ctx["retrieval_query"].startswith("Не печатает чек")
    assert ctx["client_text"] == "бумага есть"
    assert any(g.startswith("KB#") for g in ctx["grounds"])


async def test_build_agent_context_low_score_filtered():
    posts = _mk_posts()
    info = SimpleNamespace(client_id=1)

    async def fake_embed(text, task_type="query"):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def low_similar(emb, *, limit=3, query_text="", company_id=""):
        item = SimpleNamespace(id=5, content="слабое совпадение", quality="good")
        return [(item, 0.10)]                              # ниже RAG_MIN_SCORE

    async def none_wiki(title):
        return None

    async def none_pattern(equipment, keywords):
        return None

    ctx = await build_agent_context(
        posts, info, "t",
        _history_fn=lambda p, i: "H", _embed_fn=fake_embed, _similar_fn=low_similar,
        _equipment_fn=lambda t, h: None, _wiki_fn=none_wiki, _pattern_fn=none_pattern,
    )
    assert ctx["evidence"] == []
    assert ctx["grounds"] == []

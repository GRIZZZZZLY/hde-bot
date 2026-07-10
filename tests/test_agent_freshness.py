from types import SimpleNamespace

from bot.agent.freshness import check_freshness


class _FakeClient:
    def __init__(self, post_ids):
        self._posts = [SimpleNamespace(post_id=p) for p in post_ids]

    async def get_ticket_posts(self, ticket_id):
        return self._posts


async def test_current_when_latest_matches_anchor():
    client = _FakeClient([10, 20, 30])
    assert await check_freshness("T", "30", client=client) == "current"


async def test_superseded_when_new_post_arrived():
    client = _FakeClient([10, 20, 30, 31])
    assert await check_freshness("T", "30", client=client) == "superseded"


async def test_no_posts_treated_as_current():
    client = _FakeClient([])
    assert await check_freshness("T", "30", client=client) == "current"

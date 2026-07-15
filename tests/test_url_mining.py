from bot.knowledge.url_mining import (
    extract_urls,
    html_to_text,
    mine_operator_urls,
    rank_urls,
)


def test_extract_urls_strips_trailing_punct():
    text = ("Вот статья https://support.evotor.ru/article/360003913113. "
            "И ещё https://posiflora.teamly.ru/at/76aa, будут вопросы — пишите "
            "(https://clck.ru/3L4AyJ)")
    assert extract_urls(text) == [
        "https://support.evotor.ru/article/360003913113",
        "https://posiflora.teamly.ru/at/76aa",
        "https://clck.ru/3L4AyJ",
    ]


def test_rank_urls_by_frequency_top_n():
    answers = [
        "Скачайте https://rudesktop.ru/downloads/",
        "Установите https://rudesktop.ru/downloads/ и пришлите ID",
        "Статья https://support.evotor.ru/article/1",
    ]
    ranked = rank_urls(answers, top_n=1)
    assert ranked == [("https://rudesktop.ru/downloads/", 2)]


def test_html_to_text_extracts_title_and_strips_markup():
    html = ("<html><head><title>Настройка кассы</title>"
            "<style>.x{color:red}</style></head>"
            "<body><script>var a=1;</script><h1>Шаги</h1>"
            "<p>Первый&nbsp;шаг &mdash; включите кассу.</p></body></html>")
    title, text = html_to_text(html)
    assert title == "Настройка кассы"
    assert "Шаги" in text and "включите кассу" in text
    assert "var a=1" not in text and "color:red" not in text


async def test_mine_operator_urls_ranks_dedupes_and_indexes():
    answers = [
        "https://a.ru/1 и https://a.ru/1",     # 2 вхождения
        "https://b.ru/2",
        "https://known.ru/3",                  # уже в базе знаний
        "https://broken.ru/4",                 # фетч упадёт
    ]

    async def answers_fn():
        return answers

    async def fetch_fn(url):
        if "broken" in url:
            return None
        return ("Заголовок " + url, "Содержимое статьи достаточной длины " + url)

    indexed = []

    async def index_fn(source, content, *, title="", url="", content_hash="", **kw):
        indexed.append(url)
        return 1

    from bot.knowledge.url_mining import url_hash

    async def hashes_fn():
        return {url_hash("https://known.ru/3")}

    stats = await mine_operator_urls(
        top_n=10, _answers_fn=answers_fn, _fetch_fn=fetch_fn,
        _index_fn=index_fn, _hashes_fn=hashes_fn,
    )
    assert indexed == ["https://a.ru/1", "https://b.ru/2"]  # по частоте, без known/broken
    assert stats["indexed"] == 2
    assert stats["known"] == 1
    assert stats["failed"] == 1


def test_rank_urls_skips_messenger_domains():
    answers = [
        "Напишите нам https://wa.me/message/XXX и https://t.me/posiflora_II",
        "Статья https://support.evotor.ru/article/1",
    ]
    ranked = rank_urls(answers, top_n=10)
    assert ranked == [("https://support.evotor.ru/article/1", 1)]

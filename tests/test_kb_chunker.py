"""Чанкер БЗ: нарезка по заголовкам и отсев статей-оглавлений."""
from bot.knowledge.chunker import (
    chunk_markdown,
    extract_url,
    is_navigational,
    strip_frontmatter,
)

TEAMLY_HEADER = (
    "# Настройка кассы\n\n"
    "- **URL:** https://posiflora.teamly.ru/at/abc-123\n\n"
    "---\n"
)


def test_extract_url_from_teamly_frontmatter():
    assert extract_url(TEAMLY_HEADER) == "https://posiflora.teamly.ru/at/abc-123"


def test_extract_url_absent_returns_empty():
    assert extract_url("# Просто статья\n\nтекст") == ""


def test_strip_frontmatter_drops_header_and_url():
    body = strip_frontmatter(TEAMLY_HEADER + "\nПолезный текст инструкции.")
    assert body == "Полезный текст инструкции."


def test_navigational_article_detected():
    """Меню разделов — короткие строки без прозы."""
    menu = TEAMLY_HEADER + "\n" + "\n".join(
        ["Кассы", "Атол", "Эвотор", "Штрих-М", "AnyDesk", "PuTTY", "Billing"]
    )
    assert is_navigational(menu) is True


def test_real_instruction_not_navigational():
    prose = TEAMLY_HEADER + "\n" + "\n".join(
        [
            "Если касса не отвечает, проверьте физическое подключение кабеля к порту.",
            "Затем откройте драйвер АТОЛ и убедитесь, что выбран правильный COM-порт.",
            "После этого выполните тестовую печать чека и сообщите результат клиенту.",
        ]
    )
    assert is_navigational(prose) is False


def test_large_article_splits_into_multiple_chunks():
    """20 KB статья не должна остаться одним чанком — иначе поиск её не видит."""
    section = (
        "## Раздел {n}\n\n"
        + ("Подробное описание шага диагностики оборудования на точке. " * 12)
    )
    text = "\n\n".join(section.replace("{n}", str(i)) for i in range(20))
    chunks = chunk_markdown(text, target=1500, overlap=200)
    assert len(chunks) > 5
    assert all(len(c) <= 1500 * 1.6 for c in chunks)


def test_every_section_survives_chunking():
    """Уникальный термин каждого раздела должен найтись хотя бы в одном чанке."""
    text = "\n\n".join(
        f"## Раздел {i}\n\nМаркер-{i}. " + ("Текст инструкции. " * 40)
        for i in range(12)
    )
    joined = "\n".join(chunk_markdown(text))
    for i in range(12):
        assert f"Маркер-{i}." in joined


def test_short_article_stays_single_chunk():
    chunks = chunk_markdown("## Заголовок\n\nКороткая инструкция в два слова.")
    assert len(chunks) == 1


def test_oversized_single_block_is_hard_split():
    """Блок без внутренних заголовков режется по абзацам, а не теряется."""
    block = "## Один заголовок\n\n" + "\n\n".join(
        f"Абзац номер {i} с достаточно длинным текстом инструкции." for i in range(80)
    )
    chunks = chunk_markdown(block, target=1500, overlap=200)
    assert len(chunks) > 2
    assert "Абзац номер 79" in chunks[-1]

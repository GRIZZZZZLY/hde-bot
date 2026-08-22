"""Разбиение markdown-статей БЗ на чанки под эмбеддинг.

Зачем: indexer.embed_text режет текст до 8000 символов и делает один вектор на
статью. Статьи внутренней БЗ Teamly доходят до 86 KB — при таком раскладе
большая часть документа для поиска не существует. Чанкер режет по заголовкам,
чтобы каждый фрагмент остался осмысленным целиком.

Размер 1500 выбран замером (scripts/_measure_kb_scores.py, август 2026): против
600 медиана top-1 не меняется (0.863), а векторов в 2.5 раза меньше — 956 против
2409 на тех же 110 статьях.
"""
from __future__ import annotations

import re

# Целевой размер чанка в символах и перехлёст между частями большого блока.
TARGET_CHARS = 1500
OVERLAP_CHARS = 200

_HEADING_RE = re.compile(r"^#{1,6} ", re.M)

# Шапка выгрузки Teamly: "# Заголовок\n\n- **URL:** ...\n\n---\n"
_FRONTMATTER_RE = re.compile(r"^#.*?\n- \*\*URL:\*\*.*?\n---\n", re.S)

_URL_RE = re.compile(r"^- \*\*URL:\*\* (\S+)", re.M)

# Меньше трёх строк длиной 60+ символов — статья-меню, а не инструкция.
_MIN_PROSE_LINES = 3


def extract_url(text: str) -> str:
    """Вернуть URL из шапки выгрузки Teamly или пустую строку."""
    m = _URL_RE.search(text)
    return m.group(1) if m else ""


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text).strip()


def is_navigational(text: str) -> bool:
    """True для статей-оглавлений: повторённые меню разделов без прозы.

    Такие статьи — плотный набор ключевых слов ("Атол", "Эвотор", "AnyDesk"),
    который бьёт настоящую инструкцию по косинусу: в первом замере статья
    "Posiflora" (чистый список ссылок) забрала top-1 у 8 тикетов из 30.
    Размер не показатель — "Админ панель" на 12.9 KB тоже целиком меню.
    """
    body = strip_frontmatter(text)
    prose = sum(1 for line in body.splitlines() if len(line.strip()) >= 60)
    return prose < _MIN_PROSE_LINES


def _split_blocks(text: str) -> list[str]:
    """Разбить markdown на блоки, начинающиеся с заголовка."""
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    if not starts or starts[0] > 0:
        starts.insert(0, 0)
    return [
        text[a:b].strip()
        for a, b in zip(starts, starts[1:] + [len(text)])
        if text[a:b].strip()
    ]


def _hard_split(block: str, target: int, overlap: int) -> list[str]:
    """Порезать блок, который сам больше целевого размера, по абзацам."""
    paras = [p for p in re.split(r"\n\s*\n", block) if p.strip()]
    out: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) > target:
            out.append(cur)
            cur = (cur[-overlap:] + "\n\n" + p) if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur.strip():
        out.append(cur)
    return out


def chunk_markdown(
    text: str, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS
) -> list[str]:
    """Склеить блоки статьи в чанки не длиннее target символов."""
    chunks: list[str] = []
    cur = ""
    for block in _split_blocks(text):
        if len(block) > target * 1.5:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_hard_split(block, target, overlap))
            continue
        if cur and len(cur) + len(block) > target:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur.strip():
        chunks.append(cur)
    return chunks

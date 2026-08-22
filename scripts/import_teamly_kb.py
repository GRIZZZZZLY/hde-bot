"""Импорт выгрузки внутренней БЗ Teamly в knowledge_items как source='teamly'.

Идемпотентен: ключ upsert'а — синтетический ticket_id "<url|title>#<chunk_index>",
поэтому повторный прогон обновляет строки, а не плодит дубли.

Что отсеивается и почему — см. bot/knowledge/chunker.is_navigational: статьи-меню
выигрывают косинус у настоящих инструкций за счёт плотности ключевых слов.

Запуск:
    python scripts/import_teamly_kb.py --dir for_Maximus/01_knowledge_base
    python scripts/import_teamly_kb.py --dry-run     # только статистика
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("BOT_ROOT", os.getcwd()))

from bot.knowledge.chunker import (  # noqa: E402
    TARGET_CHARS,
    chunk_markdown,
    extract_url,
    is_navigational,
    strip_frontmatter,
)

SOURCE = "teamly"
EMBED_BATCH = 32


def load_articles(kb_dir: str, index_path: str) -> tuple[list[dict], list[str]]:
    """Вернуть (статьи, отсеянные_заголовки). Дедуп файлов по sha256."""
    titles_by_uuid: dict[str, str] = {}
    if os.path.exists(index_path):
        for entry in json.load(open(index_path, encoding="utf-8")):
            titles_by_uuid[entry["url"].rsplit("/", 1)[-1]] = entry["title"]

    # Дедуп по телу, а не по файлу: одна и та же статья лежит в дампе дважды —
    # под UUID и под человеческим именем. Различается только строка заголовка
    # в шапке, поэтому хэш файла их не склеивает, а хэш тела склеивает.
    by_body: dict[str, dict] = {}
    dropped: list[str] = []
    uuid_name = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")
    for name in sorted(os.listdir(kb_dir)):
        if not name.endswith(".md"):
            continue
        raw = open(os.path.join(kb_dir, name), "rb").read()
        text = raw.decode("utf-8")
        stem = re.sub(r"^\d+_", "", name[:-3])
        if is_navigational(text):
            dropped.append(stem.replace("_", " "))
            continue
        body = strip_frontmatter(text)
        url = extract_url(text)
        # Файлы дампа названы либо заголовком, либо UUID статьи — во втором
        # случае человеческое имя достаём из _index.json по этому UUID.
        title = titles_by_uuid.get(url.rsplit("/", 1)[-1]) or stem.replace("_", " ")
        digest = hashlib.sha256(body.encode()).hexdigest()
        prev = by_body.get(digest)
        # Из двух копий оставляем ту, у которой имя человеческое, а не UUID.
        if prev is None or (uuid_name.match(prev["title"]) and not uuid_name.match(title)):
            by_body[digest] = {"title": title, "url": url, "body": body}
    return list(by_body.values()), dropped


def build_chunks(articles: list[dict]) -> list[dict]:
    """Развернуть статьи в чанки с ключом идемпотентности."""
    out: list[dict] = []
    for art in articles:
        parts = chunk_markdown(art["body"], TARGET_CHARS)
        key = art["url"] or art["title"]
        for i, part in enumerate(parts):
            # Заголовок в тексте чанка: без него фрагмент из середины статьи
            # теряет тему, а промпт — понимание, откуда это.
            content = f"{art['title']}\n\n{part}"
            out.append({
                "ticket_id": f"{key}#{i}",
                "title": art["title"],
                "url": art["url"],
                "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest()[:32],
            })
    return out


async def import_chunks(chunks: list[dict]) -> tuple[int, int]:
    """Upsert чанков + батчевый эмбеддинг. Возвращает (создано, обновлено)."""
    from bot import db
    from bot.knowledge.indexer import clean_for_embedding, embed_texts
    from bot.knowledge.store import embedding_to_bytes, invalidate_embeddings_cache

    await db.init_db()

    created = updated = 0
    ids: list[int] = []
    for ch in chunks:
        item_id, was_created = await db.upsert_knowledge_item(
            source=SOURCE,
            content=ch["content"],
            ticket_id=ch["ticket_id"],
            title=ch["title"],
            url=ch["url"],
            content_hash=ch["content_hash"],
        )
        ids.append(item_id)
        created += was_created
        updated += not was_created
    print(f"upsert: создано {created}, обновлено {updated}")

    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start:start + EMBED_BATCH]
        vectors = await embed_texts(
            [clean_for_embedding(c["content"]) for c in batch], task_type="passage"
        )
        if vectors is None:
            print(f"  ОШИБКА эмбеддинга на батче {start}, прерываю")
            break
        async with db.connect() as conn:
            for item_id, vec in zip(ids[start:start + EMBED_BATCH], vectors):
                await conn.execute(
                    "UPDATE knowledge_items SET embedding=? WHERE id=?",
                    (embedding_to_bytes(vec), item_id),
                )
            await conn.commit()
        print(f"  эмбеддинг {min(start + EMBED_BATCH, len(chunks))}/{len(chunks)}")

    invalidate_embeddings_cache()
    return created, updated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="for_Maximus/01_knowledge_base")
    ap.add_argument("--index", default="for_Maximus/_index.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    articles, dropped = load_articles(args.dir, args.index)
    chunks = build_chunks(articles)
    print(f"статей к импорту: {len(articles)} | чанков: {len(chunks)}")
    print(f"отсеяно как навигационные ({len(dropped)}): {', '.join(dropped)}")
    no_url = sum(1 for c in chunks if not c["url"])
    print(f"чанков без URL (цитировать нечего): {no_url}")

    if args.dry_run:
        return
    asyncio.run(import_chunks(chunks))


if __name__ == "__main__":
    main()

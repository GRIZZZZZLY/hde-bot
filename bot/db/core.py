from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)


def db_path() -> str:
    """Call-time lookup so tests can monkeypatch bot.db.DB_PATH."""
    import bot.db as _pkg
    return _pkg.DB_PATH


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Open a DB connection with per-connection pragmas.

    busy_timeout makes concurrent writers wait instead of raising
    "database is locked"; synchronous=NORMAL is the recommended level with WAL.
    """
    async with aiosqlite.connect(db_path()) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        yield db


# ---------------------------------------------------------------------------
# Equipment name normalization
# ---------------------------------------------------------------------------

_EQUIPMENT_NORM: dict[str, str] = {
    "атол": "АТОЛ",
    "atol": "АТОЛ",
    "эвотор": "Эвотор",
    "евотор": "Эвотор",
    "эватор": "Эвотор",
    "viki": "Viki",
    "вики": "Viki",
    "vikiprint": "ВикиПринт",
    "википринт": "ВикиПринт",
    "вики принт": "ВикиПринт",
    "штрих": "Штрих-М",
    "штрих-м": "Штрих-М",
    "штрихм": "Штрих-М",
    "сбер": "Эквайринг Сбер",
    "сбербанк": "Эквайринг Сбер",
    "эквайринг сбер": "Эквайринг Сбер",
    "тинькофф": "Т-Банк",
    "тбанк": "Т-Банк",
    "т банк": "Т-Банк",
    "tinkoff": "Т-Банк",
    "t-bank": "Т-Банк",
    "tbank": "Т-Банк",
    "aqsi": "AQSI",
    "акси": "AQSI",
    "pax d230": "PAX",
    "pax q25": "PAX",
    "pax": "PAX",
    "втб": "ВТБ",
    "птк": "ПТК",
    "posiflora": "Posiflora",
    "посифлора": "Posiflora",
}


def normalize_equipment(name: str | None) -> str | None:
    """Return canonical equipment brand name, or None if name is empty."""
    if not name:
        return None
    return _EQUIPMENT_NORM.get(name.lower().strip(), name.strip() or None)

TICKET_TOPIC_COLUMNS = {
    "unique_id": "TEXT DEFAULT ''",
    "company_name": "TEXT DEFAULT ''",
    "priority": "TEXT DEFAULT ''",
    "status": "TEXT DEFAULT ''",
    "owner_id": "TEXT DEFAULT ''",
    "owner_name": "TEXT DEFAULT ''",
    "topic_state": "TEXT DEFAULT 'active'",
    "delete_after_at": "TEXT",
    "last_client_reply_at": "TEXT",
    "last_staff_reply_at": "TEXT",
    "pre_sla_notify_at": "TEXT",
    "pre_sla_sent_at": "TEXT",
    "pre_sla_message_id": "INTEGER",
    "reassurance_sent_at": "TEXT",
    "suggest_button_msg_id": "INTEGER",
    "hde_link": "TEXT DEFAULT ''",
    "updated_at": "TEXT",
    "deleted_at": "TEXT",
    "last_assigned_at": "TEXT",
    "ai_summary_sent_at": "TEXT",
    "photo_descriptions": "TEXT DEFAULT ''",
    "env_option_id": "TEXT",
    "priority_option_id": "TEXT",
    "type_option_id": "TEXT",
    # Выжимки звонков и голосовых заметок по ЭТОМУ тикету. Отдельно от
    # knowledge_items с source='transcription': там они лежат в общем retrieval
    # и всплывают в чужих обращениях, а здесь это контекст одного тикета —
    # черновик и ночная сверка читают их только для него.
    "call_notes": "TEXT DEFAULT ''",
}

UPDATABLE_FIELDS = {
    "unique_id",
    "topic_id",
    "company_name",
    "ticket_name",
    "priority",
    "status",
    "owner_id",
    "owner_name",
    "topic_state",
    "delete_after_at",
    "last_client_reply_at",
    "last_staff_reply_at",
    "pre_sla_notify_at",
    "pre_sla_sent_at",
    "pre_sla_message_id",
    "reassurance_sent_at",
    "suggest_button_msg_id",
    "hde_link",
    "deleted_at",
    "last_assigned_at",
    "ai_summary_sent_at",
    "photo_descriptions",
    "env_option_id",
    "priority_option_id",
    "type_option_id",
    "call_notes",
}


@dataclass
class TicketTopic:
    ticket_id: str
    unique_id: str
    topic_id: int
    company_name: str
    ticket_name: str
    priority: str
    status: str
    owner_id: str
    owner_name: str
    topic_state: str
    delete_after_at: Optional[str]
    last_client_reply_at: Optional[str]
    last_staff_reply_at: Optional[str]
    pre_sla_notify_at: Optional[str]
    pre_sla_sent_at: Optional[str]
    pre_sla_message_id: Optional[int]
    reassurance_sent_at: Optional[str]
    hde_link: str
    created_at: str
    updated_at: str
    deleted_at: Optional[str]
    last_assigned_at: Optional[str]
    ai_summary_sent_at: Optional[str]
    photo_descriptions: str = ""
    # Окружение autofill: None = не классифицировали, '' = не определено, цифры = option id
    env_option_id: Optional[str] = None
    # Приоритет/Тип autofill: None = не классифицировали, '' = не определено,
    # цифры = выставленный id (priority_id/type_id — top-level поля HDE)
    priority_option_id: Optional[str] = None
    type_option_id: Optional[str] = None
    # Выжимки звонков/голосовых заметок по этому тикету (см. TOPIC_COLUMNS)
    call_notes: str = ""
    # Telegram message_id последнего сообщения с кнопкой «💡 Предложить ответ»
    suggest_button_msg_id: Optional[int] = None

    @property
    def is_active(self) -> bool:
        return self.topic_state == "active"

    @property
    def is_pending_delete(self) -> bool:
        return self.topic_state == "pending_delete"

    @property
    def is_deleted(self) -> bool:
        return self.topic_state == "deleted"


@dataclass
class ReplyDraft:
    topic_id: int
    ticket_id: str
    text: str
    created_by: int
    created_at: str
    updated_at: str


@dataclass
class SentHdeMessage:
    telegram_message_id: int
    topic_id: int
    ticket_id: str
    hde_entity_id: int      # numeric ID in HDE (from response data.id)
    entity_type: str        # 'post' (public reply) or 'comment' (internal note)
    created_at: str


@dataclass
class CachedTopicMedia:
    topic_id: int
    message_id: int
    media_group_id: Optional[str]
    attachment_kind: str
    file_id: str
    filename: str
    content_type: str
    text: str
    created_at: str


@dataclass
class KnowledgeItem:
    id: int
    source: str
    ticket_id: Optional[str]
    title: Optional[str]
    content: str
    quality: str
    url: Optional[str] = None


async def init_db() -> None:
    async with connect() as db:
        # WAL is a persistent DB property: readers no longer block the writer.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_topics (
                ticket_id   TEXT PRIMARY KEY,
                topic_id    INTEGER NOT NULL,
                company     TEXT DEFAULT '',
                ticket_name TEXT DEFAULT '',
                is_closed   INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now')),
                closed_at   TEXT
            )
            """
        )
        await _ensure_ticket_topic_columns(db)
        await _migrate_legacy_ticket_topics(db)

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                event_key   TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                ticket_id   TEXT NOT NULL,
                received_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # Durable webhook inbox (ADR 2026-07-12): единственная система дедупа
        # приёма; 200 OK = событие сохранено (pending), обработано — только 'completed'.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_inbox (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        TEXT UNIQUE NOT NULL,
                payload         TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                lease_until     TEXT,
                next_attempt_at TEXT,
                last_error      TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_inbox_status "
            "ON webhook_inbox(status)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_drafts (
                topic_id    INTEGER PRIMARY KEY,
                ticket_id   TEXT NOT NULL,
                text        TEXT NOT NULL,
                created_by  INTEGER NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_media_cache (
                topic_id        INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                media_group_id  TEXT,
                attachment_kind TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                filename        TEXT DEFAULT '',
                content_type    TEXT DEFAULT '',
                text            TEXT DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (topic_id, message_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_hde_messages (
                telegram_message_id  INTEGER NOT NULL,
                topic_id             INTEGER NOT NULL,
                ticket_id            TEXT NOT NULL,
                hde_entity_id        INTEGER NOT NULL,
                entity_type          TEXT NOT NULL,
                created_at           TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (telegram_message_id, topic_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_state
            ON ticket_topics(topic_state)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_delete_after
            ON ticket_topics(topic_state, delete_after_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pre_sla
            ON ticket_topics(topic_state, pre_sla_notify_at, pre_sla_sent_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_media_group
            ON topic_media_cache(topic_id, media_group_id, message_id)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS unassigned_general_messages (
                ticket_id   TEXT PRIMARY KEY,
                message_id  INTEGER NOT NULL,
                ticket_name TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS report_runs (
                report_date TEXT PRIMARY KEY,
                ran_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                content,
                tokenize='unicode61 remove_diacritics 1'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT NOT NULL,
                ticket_id    TEXT,
                title        TEXT,
                content      TEXT NOT NULL,
                embedding    BLOB,
                quality      TEXT NOT NULL DEFAULT 'good',
                url          TEXT,
                content_hash TEXT,
                company_id   TEXT,
                company_name TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # Migrations: add columns that may be missing in existing DBs
        for col, col_type in [("company_id", "TEXT"), ("company_name", "TEXT")]:
            await _add_column_if_missing(db, "knowledge_items", col, col_type)
        # Migration: add last_used_at to knowledge_items if missing
        await _add_column_if_missing(db, "knowledge_items", "last_used_at", "TEXT")
        # Migration: add analyzed_at to track which items were processed by /aianalyze
        await _add_column_if_missing(db, "knowledge_items", "analyzed_at", "TEXT")

        # Migration: normalize equipment names in solution_patterns (idempotent)
        _norm_updates = [
            ("АТОЛ",           ["Атол", "atol", "ATOL"]),
            ("Эвотор",         ["ЭВотор", "Евотор", "Эватор"]),
            ("Штрих-М",        ["Штрих", "ШтрихМ"]),
            ("Viki",           ["Вики", "вики", "viki"]),
            ("ВикиПринт",      ["Википринт", "Viki Print", "VikiPrint"]),
            ("Эквайринг Сбер", ["Сбер", "Сбербанк"]),
            ("Т-Банк",         ["Тинькофф", "Тбанк", "Т банк", "Tinkoff", "TBank", "T-Bank"]),
            ("AQSI",           ["Акси", "акси"]),
            ("PAX",            ["PAX D230", "Pax q25", "Pax D230", "PAX d230"]),
            ("ВТБ",            ["втб", "Втб"]),
            ("ПТК",            ["птк", "Птк"]),
        ]
        # На свежей базе solution_patterns создаётся ниже в этой же init_db,
        # поэтому нормализация — no-op. Спрашиваем, есть ли таблица, вместо
        # `except Exception: pass`: тот вариант вместе с «таблицы ещё нет» глотал
        # и настоящие ошибки UPDATE.
        if await _table_exists(db, "solution_patterns"):
            for canonical, variants in _norm_updates:
                for variant in variants:
                    await db.execute(
                        "UPDATE solution_patterns SET equipment = ? WHERE equipment = ?",
                        (canonical, variant),
                    )

        # Populate FTS index for existing items (first-time migration, idempotent
        # благодаря NOT IN — повторный старт ничего не дублирует).
        # Блока try здесь больше нет: CREATE VIRTUAL TABLE выше уже падает на
        # сборке SQLite без FTS5, поэтому отказ именно на этом шаге — настоящая
        # ошибка, а проглотить её значит стартовать с пустым индексом и молча
        # деградировавшим до одного косинуса поиском.
        await db.execute(
            """
            INSERT INTO knowledge_fts(rowid, content)
            SELECT id, content FROM knowledge_items
            WHERE quality NOT IN ('bad', 'expired')
              AND id NOT IN (SELECT rowid FROM knowledge_fts)
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_feedback_pending (
                topic_id     INTEGER PRIMARY KEY,
                ticket_id    TEXT NOT NULL,
                history      TEXT NOT NULL,
                title        TEXT NOT NULL DEFAULT '',
                answer_text  TEXT NOT NULL DEFAULT '',
                ai_full_text TEXT NOT NULL DEFAULT '',
                expires_at   TEXT NOT NULL
            )
            """
        )
        # Migration: add answer_text if missing in existing DBs
        await _add_column_if_missing(
            db, "ai_feedback_pending", "answer_text", "TEXT NOT NULL DEFAULT ''"
        )
        await _add_column_if_missing(
            db, "ai_feedback_pending", "ai_full_text", "TEXT NOT NULL DEFAULT ''"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_patterns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment    TEXT,
                problem_type TEXT NOT NULL,
                steps        TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT 'analyze',
                use_count    INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_patterns_equip
            ON solution_patterns(equipment, problem_type)
            """
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS optimization_samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT NOT NULL,
                title       TEXT,
                history     TEXT NOT NULL,
                ai_answer   TEXT NOT NULL,
                op_answer   TEXT,
                outcome     TEXT NOT NULL,
                confidence  INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                score       REAL,
                proposed_by TEXT,
                status      TEXT NOT NULL DEFAULT 'candidate',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                applied_at  TEXT
            )
        """)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_suggestions (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id                TEXT NOT NULL,
                topic_id                 INTEGER,
                trigger_source           TEXT NOT NULL DEFAULT 'first',
                context_until_post_id    TEXT,
                client_id                TEXT,
                idempotency_key          TEXT UNIQUE,
                title                    TEXT DEFAULT '',
                history                  TEXT NOT NULL DEFAULT '',
                client_text              TEXT DEFAULT '',
                retrieval_query          TEXT,
                retrieval_config_version TEXT,
                embedding_model          TEXT,
                retrieved_refs           TEXT,
                pipeline_version         TEXT,
                prompt_version           TEXT,
                model                    TEXT,
                action_type              TEXT,
                ai_answer                TEXT DEFAULT '',
                ai_full_text             TEXT DEFAULT '',
                draft_answer             TEXT DEFAULT '',
                confidence               INTEGER,
                confidence_reason        TEXT,
                self_check               TEXT,
                generation_status        TEXT NOT NULL DEFAULT 'completed',
                review_status            TEXT NOT NULL DEFAULT 'pending',
                delivery_status          TEXT NOT NULL DEFAULT 'not_sent',
                evaluation_status        TEXT NOT NULL DEFAULT 'pending',
                freshness_status         TEXT NOT NULL DEFAULT 'current',
                final_sent_text          TEXT,
                final_sent_post_id       TEXT,
                reviewed_at              TEXT,
                sent_at                  TEXT,
                human_label              TEXT,
                judge_label              TEXT,
                judge_detail             TEXT,
                judge_reference_answer   TEXT,
                judged_at                TEXT,
                effective_label          TEXT,
                generation_ms            INTEGER,
                tokens_in                INTEGER,
                tokens_out               INTEGER,
                error                    TEXT,
                created_at               TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Migration: original draft answer before self-check fallback/downgrade
        await _add_column_if_missing(
            db, "ai_suggestions", "draft_answer", "TEXT DEFAULT ''"
        )
        # Migration: reconciliation verdict category (same_action / bot_escalated /
        # bot_wrong_fact) — the morning digest groups by it
        await _add_column_if_missing(db, "ai_suggestions", "judge_category", "TEXT")
        # Knowledge-base candidates queued by reconciliation (bot_wrong_fact):
        # the judge is a model, so a human decides before anything reaches the KB.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_candidates (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id    INTEGER NOT NULL UNIQUE,
                ticket_id        TEXT NOT NULL,
                title            TEXT,
                history          TEXT,
                ai_answer        TEXT,
                reference_answer TEXT,
                reason           TEXT,
                status           TEXT NOT NULL DEFAULT 'pending',
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at       TEXT
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_candidates_status "
            "ON kb_candidates(status)"
        )
        # Ревью-по-исключению (2026-09-07): кандидат проходит через выжимку в
        # обобщаемое правило, и человеку показывается только противоречие с уже
        # накопленным. rule_json хранит выжимку, чтобы решение по конфликту не
        # требовало повторного вызова модели; conflict_item_id — с каким
        # knowledge_items.id спор; kind отличает старые сырые кандидаты ('raw')
        # от новых ('rule').
        for _col, _type in [
            ("kind", "TEXT NOT NULL DEFAULT 'raw'"),
            ("rule_json", "TEXT"),
            ("conflict_item_id", "INTEGER"),
        ]:
            await _add_column_if_missing(db, "kb_candidates", _col, _type)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestions_topic ON ai_suggestions(topic_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestions_eval "
            "ON ai_suggestions(evaluation_status)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_suggestion_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id INTEGER NOT NULL,
                event_type    TEXT NOT NULL,
                payload       TEXT,
                hde_post_id   TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_suggestion_events_sid "
            "ON ai_suggestion_events(suggestion_id)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogue_pairs (
                pair_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id                 TEXT NOT NULL,
                source_message_id         TEXT,
                context_until_message_id  TEXT,
                operator_message_id       TEXT,
                operator_user_id          TEXT,
                context                   TEXT NOT NULL,
                operator_answer           TEXT NOT NULL,
                issue_type                TEXT,
                client_id                 TEXT,
                operator_answer_at        TEXT,
                resolved_at               TEXT,
                inserted_at               TEXT NOT NULL DEFAULT (datetime('now')),
                resolution_status         TEXT,
                quality_status            TEXT NOT NULL DEFAULT 'unreviewed',
                quality_reason            TEXT,
                embedding                 BLOB,
                embedding_model           TEXT,
                embedding_status          TEXT NOT NULL DEFAULT 'pending',
                embedding_text_hash       TEXT,
                content_hash              TEXT UNIQUE
            )
            """
        )
        # Migration: operator_user_id added after the table shipped (staff team ~10 people,
        # нужно отличать авторов ответов для персонального few-shot)
        await _add_column_if_missing(db, "dialogue_pairs", "operator_user_id", "TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_quality "
            "ON dialogue_pairs(quality_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_embstatus "
            "ON dialogue_pairs(embedding_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dialogue_pairs_ticket "
            "ON dialogue_pairs(ticket_id)"
        )
        await db.commit()


async def migrate_feedback_samples() -> int:
    """One-time migration: backfill optimization_samples from past 👍 feedback.

    Finds knowledge_items with source='feedback' / quality='good' that don't
    yet have a matching optimization_samples row. Returns the number added.
    Called once at startup — idempotent (skips already-migrated tickets).
    """
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT ki.ticket_id, ki.title, ki.content, ki.created_at
            FROM knowledge_items ki
            WHERE ki.source = 'feedback' AND ki.quality = 'good'
            AND NOT EXISTS (
                SELECT 1 FROM optimization_samples os
                WHERE os.ticket_id = ki.ticket_id AND os.outcome = 'accepted'
            )
        """) as cur:
            rows = await cur.fetchall()

        count = 0
        for row in rows:
            title = row["title"] or ""
            content = row["content"] or ""
            prefix = f"Тема: {title}\n\n"
            history = content[len(prefix):] if content.startswith(prefix) else content
            await db.execute(
                "INSERT INTO optimization_samples "
                "(ticket_id, title, history, ai_answer, op_answer, outcome, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["ticket_id"] or "", title, history, "", None, "accepted", row["created_at"]),
            )
            count += 1

        if count:
            await db.commit()
        return count


async def _ensure_ticket_topic_columns(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "ticket_topics")
    for name, ddl in TICKET_TOPIC_COLUMNS.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE ticket_topics ADD COLUMN {name} {ddl}")


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def _add_column_if_missing(
    db: aiosqlite.Connection, table: str, column: str, ddl: str
) -> bool:
    """ALTER TABLE только если колонки нет. True — колонку добавили.

    Раньше каждая такая миграция стояла в `try/except Exception: pass` с
    комментарием «column already exists». Это глотало ЛЮБУЮ ошибку — занятую
    базу, битый файл, кончившееся место на диске — и все они выглядели как
    «колонка уже есть»: бот стартовал на неполной схеме и падал позже, в другом
    месте и без следа причины. Спрашиваем PRAGMA (так с самого начала сделано в
    _ensure_ticket_topic_columns): тогда «колонка есть» — это ответ, а не
    пойманное исключение, и настоящая ошибка летит наверх.
    """
    if column in await _table_columns(db, table):
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    logger.info("Migration: added column %s.%s", table, column)
    return True


async def _table_columns(db: aiosqlite.Connection, table_name: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _migrate_legacy_ticket_topics(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "ticket_topics")

    if "company" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET company_name = COALESCE(NULLIF(company_name, ''), company, '')
            """
        )

    if "unique_id" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET unique_id = COALESCE(NULLIF(unique_id, ''), ticket_id)
            """
        )

    if "updated_at" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET updated_at = COALESCE(updated_at, created_at, datetime('now'))
            """
        )

    if "topic_state" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET topic_state = CASE
                WHEN topic_state IN ('active', 'pending_delete', 'deleted') THEN topic_state
                WHEN is_closed = 1 THEN 'deleted'
                ELSE 'active'
            END
            """
        )

    if "deleted_at" in columns and "closed_at" in columns:
        await db.execute(
            """
            UPDATE ticket_topics
            SET deleted_at = CASE
                WHEN topic_state = 'deleted' THEN COALESCE(deleted_at, closed_at, datetime('now'))
                ELSE deleted_at
            END
            """
        )


def _row_to_topic(row: aiosqlite.Row) -> TicketTopic:
    return TicketTopic(
        ticket_id=row["ticket_id"],
        unique_id=row["unique_id"] or row["ticket_id"],
        topic_id=row["topic_id"],
        company_name=row["company_name"] or "",
        ticket_name=row["ticket_name"],
        priority=row["priority"] or "",
        status=row["status"] or "",
        owner_id=row["owner_id"] or "",
        owner_name=row["owner_name"] or "",
        topic_state=row["topic_state"] or "active",
        delete_after_at=row["delete_after_at"],
        last_client_reply_at=row["last_client_reply_at"],
        last_staff_reply_at=row["last_staff_reply_at"],
        pre_sla_notify_at=row["pre_sla_notify_at"],
        pre_sla_sent_at=row["pre_sla_sent_at"],
        pre_sla_message_id=row["pre_sla_message_id"],
        reassurance_sent_at=row["reassurance_sent_at"] if "reassurance_sent_at" in row.keys() else None,
        suggest_button_msg_id=row["suggest_button_msg_id"] if "suggest_button_msg_id" in row.keys() else None,
        hde_link=row["hde_link"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        last_assigned_at=row["last_assigned_at"],
        ai_summary_sent_at=row["ai_summary_sent_at"] if "ai_summary_sent_at" in row.keys() else None,
        photo_descriptions=row["photo_descriptions"] if "photo_descriptions" in row.keys() else "",
        env_option_id=row["env_option_id"] if "env_option_id" in row.keys() else None,
        priority_option_id=row["priority_option_id"] if "priority_option_id" in row.keys() else None,
        type_option_id=row["type_option_id"] if "type_option_id" in row.keys() else None,
        call_notes=row["call_notes"] if "call_notes" in row.keys() else "",
    )


def _row_to_draft(row: aiosqlite.Row) -> ReplyDraft:
    return ReplyDraft(
        topic_id=row["topic_id"],
        ticket_id=row["ticket_id"],
        text=row["text"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_cached_topic_media(row: aiosqlite.Row) -> CachedTopicMedia:
    return CachedTopicMedia(
        topic_id=row["topic_id"],
        message_id=row["message_id"],
        media_group_id=row["media_group_id"],
        attachment_kind=row["attachment_kind"],
        file_id=row["file_id"],
        filename=row["filename"] or "",
        content_type=row["content_type"] or "",
        text=row["text"] or "",
        created_at=row["created_at"],
    )

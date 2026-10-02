import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_optional_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


@dataclass
class Config:
    bot_token: str
    group_chat_id: int
    personal_chat_id: int
    hde_webhook_secret: str
    hde_owner_id: str
    hde_owner_name: str
    webhook_host: str
    webhook_path_tg: str
    webhook_path_hde: str
    app_port: int
    default_reply_sla_minutes: int
    pre_sla_warning_minutes: int
    scheduler_interval_seconds: int
    hde_api_base_url: str
    hde_api_email: str
    hde_api_key: str
    hde_api_max_rpm: int
    operator_telegram_user_ids: tuple[int, ...]
    public_reply_enabled: bool
    public_reply_ticket_allowlist: tuple[str, ...]
    digest_send_hour_utc: int
    digest_night_start_hour_utc: int
    morning_digest_enabled: bool
    ticket_history_post_enabled: bool
    ai_suggestion_auto_enabled: bool
    personal_digests_enabled: bool
    nightly_reconcile_enabled: bool
    agent_voice_v2_enabled: bool
    agent_reply_drafts_enabled: bool
    reconcile_kb_distill_enabled: bool
    agent_v2_llm_base_url: str
    agent_v2_llm_api_key: str
    agent_v2_llm_model: str
    general_topic_id: int | None
    unassigned_department: str
    work_days: tuple[int, ...]
    work_hour_start: int
    work_hour_end: int
    deepgram_api_key: str
    groq_api_key: str
    openrouter_api_key: str
    presla_hde_verify: bool
    reassurance_minutes_before: int
    reassurance_text: str
    agent_enabled: bool
    agent_pipeline_version: str
    agent_auto_first_suggestion_enabled: bool
    agent_dynamic_fewshot_enabled: bool
    agent_call_fixation_enabled: bool
    agent_draft_refresh_enabled: bool
    agent_draft_model: str
    agent_selfcheck_model: str
    agent_staff_user_ids: tuple[str, ...]
    agent_dialogue_mining_enabled: bool
    groq_summary_model: str
    groq_reasoning_effort: str
    groq_vision_model: str
    groq_summary_max_tokens: int
    groq_classify_fallback_model: str
    openrouter_model: str
    optimizer_judge_model: str
    optimizer_mutation_models: tuple[str, ...]
    llm_canary_enabled: bool
    db_backup_dir: str
    db_backup_keep: int
    db_backup_hour_utc: int

    @classmethod
    def from_env(cls) -> "Config":
        # Все id моделей — из env, ни одного литерала в модулях: провайдер снимает
        # модели без предупреждения (2026-08-18: llama-3.3-70b и llama-4-scout →
        # 404 на каждом вызове), и тогда правится конфиг, а не код.
        summary_model = (
            os.getenv("GROQ_SUMMARY_MODEL", "qwen/qwen3.6-27b").strip()
            or "qwen/qwen3.6-27b"
        )
        personal_chat_id = int(os.environ["PERSONAL_CHAT_ID"])
        operator_ids = tuple(
            int(value) for value in _parse_csv(os.getenv("OPERATOR_TELEGRAM_USER_IDS"))
        ) or (personal_chat_id,)
        return cls(
            bot_token=os.environ["BOT_TOKEN"],
            group_chat_id=int(os.environ["GROUP_CHAT_ID"]),
            personal_chat_id=personal_chat_id,
            hde_webhook_secret=os.getenv("HDE_WEBHOOK_SECRET", ""),
            hde_owner_id=os.getenv("HDE_OWNER_ID", "").strip(),
            hde_owner_name=os.getenv("HDE_OWNER_NAME", "").strip(),
            webhook_host=os.environ["WEBHOOK_HOST"],
            webhook_path_tg=os.getenv("WEBHOOK_PATH_TG", "/webhook/telegram"),
            webhook_path_hde=os.getenv("WEBHOOK_PATH_HDE", "/webhook/hde"),
            app_port=int(os.getenv("APP_PORT", "8080")),
            default_reply_sla_minutes=int(os.getenv("DEFAULT_REPLY_SLA_MINUTES", "30")),
            pre_sla_warning_minutes=int(os.getenv("PRE_SLA_WARNING_MINUTES", "10")),
            scheduler_interval_seconds=int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30")),
            hde_api_base_url=os.getenv("HDE_API_BASE_URL", "").rstrip("/"),
            hde_api_email=os.getenv("HDE_API_EMAIL", "").strip(),
            hde_api_key=os.getenv("HDE_API_KEY", "").strip(),
            # Клиентский троттл: держим НИЖЕ жёсткого лимита HDE (300/min) с запасом
            # под сторонние сервисы на том же аккаунте (иначе общий бан на 20 мин).
            hde_api_max_rpm=int(os.getenv("HDE_API_MAX_RPM", "120") or 120),
            operator_telegram_user_ids=operator_ids,
            public_reply_enabled=_parse_bool(os.getenv("HDE_PUBLIC_REPLY_ENABLED"), default=False),
            public_reply_ticket_allowlist=_parse_csv(os.getenv("HDE_PUBLIC_REPLY_TICKET_ALLOWLIST")),
            digest_send_hour_utc=int(os.getenv("DIGEST_SEND_HOUR_UTC", "5")),
            digest_night_start_hour_utc=int(os.getenv("DIGEST_NIGHT_START_HOUR_UTC", "15")),
            # Тихий режим по умолчанию: оператору нужны новые ответы клиента, а
            # не пересказ того, что он и так видит в HDE. Каждый флаг включается
            # обратно в .env без правки кода.
            morning_digest_enabled=_parse_bool(
                os.getenv("MORNING_DIGEST_ENABLED"), default=False
            ),
            ticket_history_post_enabled=_parse_bool(
                os.getenv("TICKET_HISTORY_POST_ENABLED"), default=False
            ),
            ai_suggestion_auto_enabled=_parse_bool(
                os.getenv("AI_SUGGESTION_AUTO_ENABLED"), default=False
            ),
            personal_digests_enabled=_parse_bool(
                os.getenv("PERSONAL_DIGESTS_ENABLED"), default=False
            ),
            nightly_reconcile_enabled=_parse_bool(
                os.getenv("NIGHTLY_RECONCILE_ENABLED"), default=False
            ),
            # Агент черновиков v2 (spec 2026-09-27): короткий промпт по регламенту,
            # проверки кодом вместо self-check, одно сообщение-черновик.
            agent_voice_v2_enabled=_parse_bool(
                os.getenv("AGENT_VOICE_V2_ENABLED"), default=False
            ),
            # Черновик на каждый новый ответ клиента (дописывается в его сообщение).
            agent_reply_drafts_enabled=_parse_bool(
                os.getenv("AGENT_REPLY_DRAFTS_ENABLED"), default=False
            ),
            # Пополнение базы знаний после ночной сверки. Отдельно от самой сверки:
            # замер включён, а база сама не пополняется (решение тихого режима).
            reconcile_kb_distill_enabled=_parse_bool(
                os.getenv("RECONCILE_KB_DISTILL_ENABLED"), default=False
            ),
            # Другой OpenAI-совместимый провайдер только для черновиков v2. Пусто —
            # v2 ходит в Groq с AGENT_DRAFT_MODEL, как старый путь.
            agent_v2_llm_base_url=os.getenv("AGENT_V2_LLM_BASE_URL", "").strip(),
            agent_v2_llm_api_key=os.getenv("AGENT_V2_LLM_API_KEY", "").strip(),
            agent_v2_llm_model=os.getenv("AGENT_V2_LLM_MODEL", "").strip(),
            general_topic_id=_parse_optional_int(os.getenv("GENERAL_TOPIC_ID")),
            unassigned_department=os.getenv("UNASSIGNED_DEPARTMENT", "").strip(),
            work_days=tuple(
                int(d) for d in _parse_csv(os.getenv("WORK_DAYS", "0,1,2,3,6"))
            ),
            work_hour_start=int(os.getenv("WORK_HOUR_START", "9")),
            work_hour_end=int(os.getenv("WORK_HOUR_END", "18")),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", "").strip(),
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            presla_hde_verify=os.getenv("PRESLA_HDE_VERIFY", "1") == "1",
            reassurance_minutes_before=int(os.getenv("REASSURANCE_MINUTES_BEFORE", "2")),
            reassurance_text=os.getenv(
                "REASSURANCE_TEXT",
                "Я про вас не забыл, занимаюсь вашим вопросом",
            ),
            agent_enabled=_parse_bool(os.getenv("AGENT_ENABLED"), default=False),
            agent_pipeline_version=os.getenv("AGENT_PIPELINE_VERSION", "v0").strip() or "v0",
            agent_auto_first_suggestion_enabled=_parse_bool(
                os.getenv("AGENT_AUTO_FIRST_SUGGESTION_ENABLED"), default=False
            ),
            agent_dynamic_fewshot_enabled=_parse_bool(
                os.getenv("AGENT_DYNAMIC_FEWSHOT_ENABLED"), default=False
            ),
            agent_call_fixation_enabled=_parse_bool(
                os.getenv("AGENT_CALL_FIXATION_ENABLED"), default=False
            ),
            # Пересборка черновика по новому комментарию коллеги. Своя ручка, а
            # не общий agent_enabled: пересборка тратит токены Groq, и на
            # free-tier TPM это заметно (см. bot/agent/draft_refresh.py).
            agent_draft_refresh_enabled=_parse_bool(
                os.getenv("AGENT_DRAFT_REFRESH_ENABLED"), default=False
            ),
            agent_draft_model=os.getenv(
                "AGENT_DRAFT_MODEL", "qwen/qwen3.6-27b"
            ).strip() or "qwen/qwen3.6-27b",
            agent_selfcheck_model=os.getenv(
                "AGENT_SELFCHECK_MODEL", "openai/gpt-oss-120b"
            ).strip() or "openai/gpt-oss-120b",
            agent_staff_user_ids=_parse_csv(os.getenv("AGENT_STAFF_USER_IDS")),
            agent_dialogue_mining_enabled=_parse_bool(
                os.getenv("AGENT_DIALOGUE_MINING_ENABLED"), default=False
            ),
            groq_summary_model=summary_model,
            groq_reasoning_effort=os.getenv("GROQ_REASONING_EFFORT", "").strip(),
            # Мультимодальная модель для картинок. Дефолт — модель суммарки:
            # сейчас она мультимодальна, но роль отдельная, поэтому и env свой.
            groq_vision_model=os.getenv("GROQ_VISION_MODEL", "").strip() or summary_model,
            # max_tokens РЕЗЕРВИРУЕТСЯ против TPM: провайдер считает
            # prompt + max_tokens, поэтому запас «на всякий случай» напрямую
            # уменьшает число вызовов в минуту. Прод-замер: самый длинный ответ
            # 612 символов (~250 токенов), так что 700 — трёхкратный запас.
            groq_summary_max_tokens=int(os.getenv("GROQ_SUMMARY_MAX_TOKENS", "700") or 700),
            # Фолбэк классификаторов: живёт на ОТДЕЛЬНОЙ per-model квоте Groq,
            # поэтому переживает 429 основной модели.
            groq_classify_fallback_model=os.getenv(
                "GROQ_CLASSIFY_FALLBACK_MODEL", "openai/gpt-oss-20b"
            ).strip() or "openai/gpt-oss-20b",
            openrouter_model=os.getenv(
                "OPENROUTER_MODEL", "google/gemma-4-31b-it:free"
            ).strip() or "google/gemma-4-31b-it:free",
            # Судья держится ДРУГОГО семейства, чем генератор (qwen) — иначе
            # модель поощряет собственный стиль (self-preference).
            optimizer_judge_model=os.getenv(
                "OPTIMIZER_JUDGE_MODEL", "openai/gpt-oss-120b"
            ).strip() or "openai/gpt-oss-120b",
            # Разные семейства специально: разнообразие мутаций + раздельные
            # per-model квоты Groq.
            optimizer_mutation_models=_parse_csv(
                os.getenv("OPTIMIZER_MUTATION_MODELS")
            ) or ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"),
            llm_canary_enabled=_parse_bool(os.getenv("LLM_CANARY_ENABLED"), default=True),
            # Пустой DB_BACKUP_DIR выключает бэкап. Копий три, а не семь: база
            # 242 МБ и почти вся — плохо сжимаемые эмбеддинги float32.
            db_backup_dir=os.getenv("DB_BACKUP_DIR", "backups").strip(),
            db_backup_keep=int(os.getenv("DB_BACKUP_KEEP", "3") or 3),
            db_backup_hour_utc=int(os.getenv("DB_BACKUP_HOUR_UTC", "1") or 1),
        )

    def matches_owner(self, owner_id: str, owner_name: str) -> bool:
        if self.hde_owner_id and owner_id:
            return str(owner_id).strip() == self.hde_owner_id
        if self.hde_owner_name and owner_name:
            return owner_name.strip().lower() == self.hde_owner_name.lower()
        # No owner filter configured — accept all assignments
        if not self.hde_owner_id and not self.hde_owner_name:
            return True
        return False

    def has_hde_api_credentials(self) -> bool:
        return bool(self.hde_api_base_url and self.hde_api_email and self.hde_api_key)

    def is_operator_allowed(self, telegram_user_id: int) -> bool:
        from .operators import by_tg_user
        return telegram_user_id in self.operator_telegram_user_ids or by_tg_user(telegram_user_id) is not None

    def is_known_chat(self, chat_id: int) -> bool:
        """The bot answers only in operators' groups and private chats."""
        from .operators import OPERATORS
        known = {self.group_chat_id, self.personal_chat_id, *self.operator_telegram_user_ids}
        known.update(c for o in OPERATORS for c in (o.chat_id, o.tg_user_id))
        return chat_id in known

    def is_public_reply_allowed(self, ticket_id: str, unique_id: str) -> bool:
        if not self.public_reply_ticket_allowlist:
            return True
        allowed = set(self.public_reply_ticket_allowlist)
        return ticket_id in allowed or unique_id in allowed


config = Config.from_env()

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
    operator_telegram_user_ids: tuple[int, ...]
    public_reply_enabled: bool
    public_reply_ticket_allowlist: tuple[str, ...]
    digest_send_hour_utc: int
    digest_night_start_hour_utc: int
    general_topic_id: int | None
    unassigned_department: str

    @classmethod
    def from_env(cls) -> "Config":
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
            operator_telegram_user_ids=operator_ids,
            public_reply_enabled=_parse_bool(os.getenv("HDE_PUBLIC_REPLY_ENABLED"), default=False),
            public_reply_ticket_allowlist=_parse_csv(os.getenv("HDE_PUBLIC_REPLY_TICKET_ALLOWLIST")),
            digest_send_hour_utc=int(os.getenv("DIGEST_SEND_HOUR_UTC", "5")),
            digest_night_start_hour_utc=int(os.getenv("DIGEST_NIGHT_START_HOUR_UTC", "15")),
            general_topic_id=_parse_optional_int(os.getenv("GENERAL_TOPIC_ID")),
            unassigned_department=os.getenv("UNASSIGNED_DEPARTMENT", "").strip(),
        )

    def matches_owner(self, owner_id: str, owner_name: str) -> bool:
        if self.hde_owner_id and owner_id:
            return str(owner_id).strip() == self.hde_owner_id
        if self.hde_owner_name and owner_name:
            return owner_name.strip().lower() == self.hde_owner_name.lower()
        return False

    def has_hde_api_credentials(self) -> bool:
        return bool(self.hde_api_base_url and self.hde_api_email and self.hde_api_key)

    def is_operator_allowed(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.operator_telegram_user_ids

    def is_public_reply_allowed(self, ticket_id: str, unique_id: str) -> bool:
        if not self.public_reply_ticket_allowlist:
            return True
        allowed = set(self.public_reply_ticket_allowlist)
        return ticket_id in allowed or unique_id in allowed


config = Config.from_env()

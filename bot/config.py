"""Конфигурация из переменных окружения. Ничего не хардкодим."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str

    graph_tenant_id: str
    graph_client_id: str
    graph_client_secret: str
    planner_plan_id: str
    planner_bucket_id: str
    planner_bucket_name: str
    planner_enabled: bool

    marketing_chat_id: str
    max_attachments: int

    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout: int
    llm_normalize: bool

    requests_log_path: Path
    fallback_log_path: Path
    requests_db_path: Path
    log_level: str

    def validate(self) -> list[str]:
        """Возвращает список фатальных проблем конфигурации."""
        problems: list[str] = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if self.planner_enabled:
            for name, value in (
                ("GRAPH_TENANT_ID", self.graph_tenant_id),
                ("GRAPH_CLIENT_ID", self.graph_client_id),
                ("GRAPH_CLIENT_SECRET", self.graph_client_secret),
                ("PLANNER_PLAN_ID", self.planner_plan_id),
            ):
                if not value:
                    problems.append(f"{name} не задан (нужен при PLANNER_ENABLED=true)")
        return problems


def load_config() -> Config:
    data_dir = Path(_str("DATA_DIR", "/data"))
    return Config(
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        graph_tenant_id=_str("GRAPH_TENANT_ID"),
        graph_client_id=_str("GRAPH_CLIENT_ID"),
        graph_client_secret=_str("GRAPH_CLIENT_SECRET"),
        planner_plan_id=_str("PLANNER_PLAN_ID"),
        planner_bucket_id=_str("PLANNER_BUCKET_ID"),
        planner_bucket_name=_str("PLANNER_BUCKET_NAME", "Новые задачи"),
        planner_enabled=_bool("PLANNER_ENABLED", True),
        marketing_chat_id=_str("MARKETING_CHAT_ID"),
        max_attachments=_int("MAX_ATTACHMENTS", 10),
        llm_base_url=_str("LLM_BASE_URL").rstrip("/"),
        llm_model=_str("LLM_MODEL"),
        llm_api_key=_str("LLM_API_KEY", "not-needed"),
        llm_timeout=_int("LLM_TIMEOUT", 120),
        llm_normalize=_bool("LLM_NORMALIZE", True),
        requests_log_path=data_dir / "requests.jsonl",
        fallback_log_path=data_dir / "planner_fallback.jsonl",
        requests_db_path=Path(_str("REQUESTS_DB_PATH", str(data_dir / "requests.db"))),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )

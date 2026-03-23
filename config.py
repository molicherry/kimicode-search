import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_WEBUI_ENABLED = False
DEFAULT_DB_PATH = "data/app.db"
DEFAULT_SESSION_COOKIE_NAME = "kimi_admin_session"
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
DEFAULT_BOOTSTRAP_ADMIN_USERNAME = "admin"
DEFAULT_BOOTSTRAP_ADMIN_PASSWORD = "admin123456"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppConfig:
    webui_enabled: bool
    db_path: Path
    session_secret: str
    session_cookie_name: str
    session_ttl_seconds: int
    bootstrap_admin_username: str
    bootstrap_admin_password: str


def load_app_config() -> AppConfig:
    return AppConfig(
        webui_enabled=env_flag("KIMI_WEBUI_ENABLED", DEFAULT_WEBUI_ENABLED),
        db_path=Path(os.getenv("KIMI_DB_PATH", DEFAULT_DB_PATH)),
        session_secret=os.getenv("KIMI_SESSION_SECRET", "dev-session-secret-change-me"),
        session_cookie_name=os.getenv("KIMI_SESSION_COOKIE_NAME", DEFAULT_SESSION_COOKIE_NAME),
        session_ttl_seconds=env_int("KIMI_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS),
        bootstrap_admin_username=os.getenv(
            "KIMI_BOOTSTRAP_ADMIN_USERNAME",
            DEFAULT_BOOTSTRAP_ADMIN_USERNAME,
        ),
        bootstrap_admin_password=os.getenv(
            "KIMI_BOOTSTRAP_ADMIN_PASSWORD",
            DEFAULT_BOOTSTRAP_ADMIN_PASSWORD,
        ),
    )

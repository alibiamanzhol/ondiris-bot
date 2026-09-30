import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().replace(".", ":").split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class Config:
    token: str
    db_path: Path
    monitor_time: time
    tz: ZoneInfo
    allowed_user_ids: frozenset[int]
    portal_url: str
    portal_concurrency: int
    legacy_state_file: Path


def load_config() -> Config:
    load_dotenv()
    allowed = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
    return Config(
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        db_path=Path(os.getenv("DB_PATH", "data/bot.db")),
        monitor_time=parse_hhmm(os.getenv("MONITOR_TIME", "18:00")),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty")),
        allowed_user_ids=frozenset(int(x) for x in allowed.replace(";", ",").split(",") if x.strip()),
        portal_url=os.getenv("PORTAL_API_URL", "https://e-ondiris.gov.kz/awp-api/registry-front"),
        portal_concurrency=int(os.getenv("PORTAL_CONCURRENCY", "3")),
        legacy_state_file=Path(os.getenv("LEGACY_STATE_FILE", "state.json")),
    )

import os
from typing import List

from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CHANNEL_ID: str = os.getenv("CHANNEL_ID", "")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DIGEST_HOUR: int = int(os.getenv("DIGEST_HOUR", "10"))
DIGEST_MINUTE: int = int(os.getenv("DIGEST_MINUTE", "0"))

_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = [int(x) for x in _raw.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID не задан в .env")

import re

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_channel(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:www\.)?t\.me/", "", value, flags=re.IGNORECASE)
    value = value.lstrip("@").rstrip("/")
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError("Нужен username публичного Telegram-канала, например @hse_events")
    return value.lower()


def parse_channel_command(text: str) -> str:
    parts = text.split()
    if len(parts) != 2:
        raise ValueError("Укажите ровно один канал: /parser_add @channel")
    return normalize_channel(parts[1])

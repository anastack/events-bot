"""
Channel Parser — автономный скрипт мониторинга Telegram-каналов.
Извлекает мероприятия из постов с помощью AI (OpenRouter),
проверяет дубликаты и сохраняет в общую БД.

Полностью независим от bot.py — не импортирует никакие модули бота.
Работает с той же базой данных.

Запуск:  python channel_parser.py
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto

# ── config ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
PARSER_USER_ID = int(os.getenv("PARSER_USER_ID", "0"))
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "")

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = [int(x) for x in _raw_admins.split(",") if x.strip().isdigit()]

_raw_channels = os.getenv("PARSER_CHANNELS", "")
PARSER_CHANNELS: List[str] = [ch.strip() for ch in _raw_channels.split(",") if ch.strip()]

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("channel_parser")

# ── validation ────────────────────────────────────────────────────────────────

def _validate_config() -> None:
    errors = []
    if not TELEGRAM_API_ID:
        errors.append("TELEGRAM_API_ID не задан в .env")
    if not TELEGRAM_API_HASH:
        errors.append("TELEGRAM_API_HASH не задан в .env")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN не задан в .env")
    if not OPENROUTER_API_KEY:
        errors.append("OPENROUTER_API_KEY не задан в .env")
    if not PARSER_CHANNELS:
        errors.append("PARSER_CHANNELS не задан в .env")
    if not ADMIN_IDS:
        errors.append("ADMIN_IDS не задан в .env (парсер требует админов для проверки)")
    if errors:
        for err in errors:
            logger.error("❌ %s", err)
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE — автономная работа с БД (та же БД, что и бот)
# ══════════════════════════════════════════════════════════════════════════════

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = "postgresql://" + _DATABASE_URL[len("postgres://"):]

_USE_PG = _DATABASE_URL.startswith("postgresql://")

if _USE_PG:
    import psycopg2
else:
    _DB_PATH = os.environ.get(
        "DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.db"),
    )


def _ph(sql: str) -> str:
    return sql.replace("?", "%s") if _USE_PG else sql


@contextmanager
def _conn():
    if _USE_PG:
        c = psycopg2.connect(_DATABASE_URL)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        with sqlite3.connect(_DB_PATH) as c:
            yield c


def _fetch(sql: str, params: tuple = ()) -> List[Tuple]:
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(_ph(sql), params)
            return cur.fetchall()
        return c.execute(_ph(sql), params).fetchall()


def _fetchone(sql: str, params: tuple = ()) -> Optional[Tuple]:
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(_ph(sql), params)
            return cur.fetchone()
        return c.execute(_ph(sql), params).fetchone()


def _init_parser_table() -> None:
    """Создать таблицу parsed_posts если не существует."""
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute("""
                CREATE TABLE IF NOT EXISTS parsed_posts (
                    channel_id   BIGINT  NOT NULL,
                    message_id   INTEGER NOT NULL,
                    post_hash    TEXT    NOT NULL,
                    processed_at TEXT    NOT NULL,
                    event_id     INTEGER,
                    PRIMARY KEY (channel_id, message_id)
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS parsed_posts (
                    channel_id   INTEGER NOT NULL,
                    message_id   INTEGER NOT NULL,
                    post_hash    TEXT    NOT NULL,
                    processed_at TEXT    NOT NULL,
                    event_id     INTEGER,
                    PRIMARY KEY (channel_id, message_id)
                )
            """)
    logger.info(
        "DB ready (%s). Таблица parsed_posts ОК.",
        f"PostgreSQL" if _USE_PG else f"SQLite: {_DB_PATH}",
    )


def is_post_parsed(channel_id: int, message_id: int) -> bool:
    return _fetchone(
        "SELECT 1 FROM parsed_posts WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ) is not None


def mark_post_parsed(
    channel_id: int, message_id: int, post_hash: str,
    event_id: Optional[int] = None,
) -> None:
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute(
                "INSERT INTO parsed_posts (channel_id, message_id, post_hash, processed_at, event_id) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (channel_id, message_id, post_hash, datetime.now().isoformat(), event_id),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO parsed_posts "
                "(channel_id, message_id, post_hash, processed_at, event_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, message_id, post_hash, datetime.now().isoformat(), event_id),
            )


def add_event(
    name: str, date_display: str, date_sort: str, location: str,
    link: Optional[str], description: Optional[str],
    photo_file_id: Optional[str], user_id: int,
    event_type: Optional[str] = None, topic: Optional[str] = None,
    status: str = "pending",
) -> int:
    sql = _ph("""
        INSERT INTO events
            (name, date_display, date_sort, location, link, description,
             photo_file_id, user_id, created_at, event_type, topic, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """)
    params = (
        name, date_display, date_sort, location, link, description,
        photo_file_id, user_id, datetime.now().isoformat(),
        event_type, topic, status,
    )
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(sql + " RETURNING id", params)
            return cur.fetchone()[0]
        else:
            cur = c.execute(sql, params)
            return cur.lastrowid


def get_recent_events_for_dedup(date_sort: str) -> List[Tuple]:
    """Получить pending/approved события ±7 дней от указанной даты."""
    try:
        d = datetime.strptime(date_sort, "%Y-%m-%d")
    except ValueError:
        return []
    start = (d - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (d + timedelta(days=7)).strftime("%Y-%m-%d")
    return _fetch(
        "SELECT id, name, date_display, date_sort, location, description, event_type, topic "
        "FROM events "
        "WHERE date_sort >= ? AND date_sort <= ? "
        "AND status IN ('approved', 'pending') "
        "ORDER BY date_sort ASC",
        (start, end),
    )


# ══════════════════════════════════════════════════════════════════════════════
# AI — OpenRouter API
# ══════════════════════════════════════════════════════════════════════════════

CLASSIFY_SYSTEM_PROMPT = """\
Ты анализируешь посты из Telegram-каналов. Определи, является ли пост \
анонсом конкретного мероприятия (концерт, лекция, выставка, вечеринка, \
форум, спектакль, мастер-класс, митап и т.д.).

КРИТЕРИИ МЕРОПРИЯТИЯ:
- Должна быть конкретная дата (или хотя бы месяц)
- Должно быть понятно, что это разовое событие, а не реклама/новость/обзор
- Рекламные посты, новости, обзоры, подборки — НЕ мероприятия

Если пост НЕ является мероприятием — верни ТОЛЬКО:
{"is_event": false}

Если пост ЯВЛЯЕТСЯ мероприятием — верни JSON:
{
  "is_event": true,
  "name": "Название мероприятия (краткое, без лишних слов)",
  "date_display": "15 июня 2025, 19:00",
  "date_sort": "2025-06-15",
  "location": "Место проведения (адрес или название площадки)",
  "link": "https://... или null если нет ссылки",
  "description": "Краткое описание мероприятия, до 500 символов",
  "event_type": "одно из: Концерт|Лекция|Форум|Вечеринка|Выставка|Спектакль|Другое",
  "topic": "одно из: Бизнес|Образование|Студенческие|Культура|Спорт|Технологии|Другое"
}

ПРАВИЛА:
- date_display: формат "день месяцРУ год, ЧЧ:ММ" (пример: "15 июня 2025, 19:00")
- date_sort: формат YYYY-MM-DD
- Если время не указано, в date_display укажи только дату без времени
- Если дату определить невозможно — верни {"is_event": false}
- event_type и topic выбирай СТРОГО из предложенных вариантов
- description — краткое содержательное описание, до 500 символов
- location: если место не указано, напиши "Уточняется"
- Отвечай ТОЛЬКО валидным JSON, без markdown-обёрток и комментариев
"""

DEDUP_SYSTEM_PROMPT = """\
Ты проверяешь, является ли новое мероприятие дубликатом уже существующего \
в базе данных. Два мероприятия считаются дубликатами, если это ОДНО И ТО ЖЕ \
событие, даже если описание и формулировки отличаются.

КРИТЕРИИ ДУБЛИКАТА:
- Совпадает суть мероприятия (одно и то же событие)
- Дата совпадает или очень близка (±1 день)
- Место похоже или совпадает

НЕ ДУБЛИКАТЫ:
- Разные мероприятия в один день
- Похожие по тематике, но разные события
- Регулярные мероприятия (каждое — отдельное)

Ответь ТОЛЬКО валидным JSON:
{"is_duplicate": true, "duplicate_of_id": 123}
или
{"is_duplicate": false}
"""


async def _call_openrouter(
    system_prompt: str,
    user_message: str,
    http_client: httpx.AsyncClient,
) -> Optional[dict]:
    """Вызов OpenRouter API, возврат распарсенного JSON."""
    try:
        resp = await http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # Убрать markdown code blocks если есть
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        content = content.strip()

        return json.loads(content)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "OpenRouter HTTP %d: %s",
            exc.response.status_code, exc.response.text[:300],
        )
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.error("Не удалось разобрать ответ OpenRouter: %s", exc)
        return None
    except Exception as exc:
        logger.error("Ошибка вызова OpenRouter: %s", exc)
        return None


async def classify_post(text: str, http_client: httpx.AsyncClient) -> Optional[dict]:
    """Определить, является ли пост мероприятием, и извлечь данные."""
    today = datetime.now().strftime("%Y-%m-%d")
    user_msg = f"Сегодняшняя дата: {today}\n\nТекст поста из Telegram-канала:\n\n{text}"

    result = await _call_openrouter(CLASSIFY_SYSTEM_PROMPT, user_msg, http_client)
    if result is None:
        return None
    if not result.get("is_event", False):
        return None

    # Проверка обязательных полей
    for field in ("name", "date_display", "date_sort", "location"):
        if not result.get(field):
            logger.warning("AI вернул мероприятие без поля '%s'", field)
            return None
    return result


async def check_duplicate(
    event_data: dict,
    existing_events: List[Tuple],
    http_client: httpx.AsyncClient,
) -> Optional[int]:
    """Проверить дубликат. Возвращает ID существующего события или None."""
    if not existing_events:
        return None

    existing_lines = []
    for ev in existing_events:
        ev_id, name, date_display, date_sort, location, description, *_ = ev
        line = f"ID={ev_id} | {name} | {date_display} | {location}"
        if description:
            line += f" | {description[:100]}"
        existing_lines.append(line)

    user_msg = (
        f"НОВОЕ МЕРОПРИЯТИЕ:\n"
        f"Название: {event_data['name']}\n"
        f"Дата: {event_data['date_display']}\n"
        f"Место: {event_data['location']}\n"
        f"Описание: {event_data.get('description', 'нет')}\n\n"
        f"СУЩЕСТВУЮЩИЕ МЕРОПРИЯТИЯ В БАЗЕ:\n"
        + "\n".join(existing_lines)
    )

    result = await _call_openrouter(DEDUP_SYSTEM_PROMPT, user_msg, http_client)
    if result and result.get("is_duplicate", False):
        dup_id = result.get("duplicate_of_id")
        logger.info("🔄 Дубликат: '%s' → событие #%s", event_data["name"], dup_id)
        return dup_id
    return None


# ══════════════════════════════════════════════════════════════════════════════
# BOT API — уведомления админам через HTTP (без python-telegram-bot)
# ══════════════════════════════════════════════════════════════════════════════

def _e(text: str) -> str:
    return html.escape(str(text))


async def _bot_api(
    method: str, payload: dict, http_client: httpx.AsyncClient,
) -> Optional[dict]:
    """Вызвать метод Telegram Bot API."""
    try:
        resp = await http_client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=30.0,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("Bot API %s error: %s", method, data)
            return None
        return data.get("result")
    except Exception as exc:
        logger.error("Bot API %s failed: %s", method, exc)
        return None


async def upload_photo_via_bot(
    photo_bytes: bytes, http_client: httpx.AsyncClient,
) -> Optional[str]:
    """Загрузить фото через Bot API и получить file_id."""
    if not ADMIN_IDS:
        return None
    admin_id = ADMIN_IDS[0]
    try:
        resp = await http_client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": str(admin_id)},
            files={"photo": ("event.jpg", photo_bytes, "image/jpeg")},
            timeout=30.0,
        )
        result = resp.json()
        if not result.get("ok"):
            logger.error("Bot API sendPhoto failed: %s", result)
            return None
        photos = result["result"].get("photo", [])
        if not photos:
            return None
        file_id = photos[-1]["file_id"]
        # Удалить временное сообщение
        msg_id = result["result"]["message_id"]
        await _bot_api("deleteMessage", {"chat_id": admin_id, "message_id": msg_id}, http_client)
        return file_id
    except Exception as exc:
        logger.error("Не удалось загрузить фото через Bot API: %s", exc)
        return None


async def notify_admins(
    event_data: dict, event_id: int,
    source_channel: str, http_client: httpx.AsyncClient,
) -> None:
    """Отправить уведомление админам о новом мероприятии для проверки."""
    # Форматирование поста (тот же формат, что и в боте)
    lines = [f"🎉 <b>{_e(event_data['name'])}</b>"]
    meta = []
    if event_data.get("event_type"):
        meta.append(f"🎪 {_e(event_data['event_type'])}")
    if event_data.get("topic"):
        meta.append(f"🏷 {_e(event_data['topic'])}")
    if meta:
        lines += ["", "  •  ".join(meta)]
    if event_data.get("description"):
        lines += ["", _e(event_data["description"])]
    lines += [
        "",
        f"📅 <b>Дата:</b> {_e(event_data['date_display'])}",
        f"📍 <b>Место:</b> {_e(event_data['location'])}",
    ]
    link = event_data.get("link")
    if link:
        if link.startswith(("http://", "https://")):
            lines.append(f'🎟 <b>Ссылка:</b> <a href="{_e(link)}">Перейти →</a>')
        else:
            lines.append(f"🎟 <b>Ссылка:</b> {_e(link)}")

    header = (
        f"🤖 <b>Новое мероприятие из парсера</b>\n"
        f"📢 Канал: {_e(source_channel)}\n\n"
    )
    post_text = header + "\n".join(lines)

    # Кнопки одобрения — совместимы с approval_callback в bot.py
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Одобрить", "callback_data": f"appr:ok:{event_id}"},
            {"text": "❌ Отклонить", "callback_data": f"appr:no:{event_id}"},
        ]]
    }

    photo_file_id = event_data.get("photo_file_id")

    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id and len(post_text) <= 1024:
                await _bot_api("sendPhoto", {
                    "chat_id": admin_id,
                    "photo": photo_file_id,
                    "caption": post_text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                }, http_client)
            elif photo_file_id:
                await _bot_api("sendPhoto", {
                    "chat_id": admin_id, "photo": photo_file_id,
                }, http_client)
                await _bot_api("sendMessage", {
                    "chat_id": admin_id,
                    "text": post_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": keyboard,
                }, http_client)
            else:
                await _bot_api("sendMessage", {
                    "chat_id": admin_id,
                    "text": post_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": keyboard,
                }, http_client)
        except Exception as exc:
            logger.error("Не удалось уведомить админа %d: %s", admin_id, exc)


# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА ПОСТОВ
# ══════════════════════════════════════════════════════════════════════════════

def _post_hash(channel_id: int, text: str) -> str:
    raw = f"{channel_id}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def process_message(
    event: events.NewMessage.Event,
    telethon_client: TelegramClient,
    http_client: httpx.AsyncClient,
) -> None:
    """Обработать одно новое сообщение из канала."""
    msg = event.message
    chat = await event.get_chat()
    channel_id = chat.id
    message_id = msg.id
    channel_title = getattr(chat, "title", None) or f"id:{channel_id}"
    channel_username = getattr(chat, "username", None)
    source_label = f"@{channel_username}" if channel_username else channel_title

    # Текст поста
    text = msg.text or msg.message or ""
    if not text or len(text) < 20:
        return  # Слишком короткий

    # Уже обработан?
    if is_post_parsed(channel_id, message_id):
        return

    post_h = _post_hash(channel_id, text)
    logger.info(
        "📨 Новый пост из %s (msg_id=%d, %d символов)",
        source_label, message_id, len(text),
    )

    # ── Шаг 1: Классификация AI ──────────────────────────────────────────
    event_data = await classify_post(text, http_client)
    if event_data is None:
        logger.info("   ↳ Не мероприятие, пропускаем")
        mark_post_parsed(channel_id, message_id, post_h)
        return

    logger.info(
        "   ↳ 🎉 Мероприятие: %s (%s)",
        event_data["name"], event_data["date_display"],
    )

    # ── Шаг 2: Проверка дубликатов ────────────────────────────────────────
    existing = get_recent_events_for_dedup(event_data["date_sort"])
    dup_id = await check_duplicate(event_data, existing, http_client)
    if dup_id is not None:
        logger.info("   ↳ 🔄 Дубликат события #%d, пропускаем", dup_id)
        mark_post_parsed(channel_id, message_id, post_h, event_id=dup_id)
        return

    # ── Шаг 3: Обработка фото ────────────────────────────────────────────
    photo_file_id = None
    if msg.media and isinstance(msg.media, MessageMediaPhoto):
        try:
            photo_data = await telethon_client.download_media(msg.media, bytes)
            if photo_data:
                photo_file_id = await upload_photo_via_bot(photo_data, http_client)
                if photo_file_id:
                    logger.info("   ↳ 📸 Фото загружено")
        except Exception as exc:
            logger.warning("   ↳ ⚠️ Не удалось загрузить фото: %s", exc)

    event_data["photo_file_id"] = photo_file_id

    # ── Шаг 4: Сохранение в БД ───────────────────────────────────────────
    new_event_id = add_event(
        name=event_data["name"],
        date_display=event_data["date_display"],
        date_sort=event_data["date_sort"],
        location=event_data["location"],
        link=event_data.get("link"),
        description=event_data.get("description"),
        photo_file_id=photo_file_id,
        user_id=PARSER_USER_ID,
        event_type=event_data.get("event_type"),
        topic=event_data.get("topic"),
        status="pending",
    )
    logger.info("   ↳ 💾 Сохранено (event_id=%d, status=pending)", new_event_id)

    # Пометить пост как обработанный
    mark_post_parsed(channel_id, message_id, post_h, event_id=new_event_id)

    # ── Шаг 5: Уведомление админам ───────────────────────────────────────
    await notify_admins(event_data, new_event_id, source_label, http_client)
    logger.info("   ↳ 📩 Уведомление отправлено админам")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    _validate_config()
    _init_parser_table()

    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("  Channel Parser v1.0")
    logger.info("  Каналов: %d | Модель: %s", len(PARSER_CHANNELS), OPENROUTER_MODEL)
    logger.info("  Админы: %s", ADMIN_IDS)
    logger.info("═══════════════════════════════════════════════════════════")

    # Telethon клиент — StringSession (Railway) или файловая сессия (локально)
    if TELETHON_SESSION:
        session = StringSession(TELETHON_SESSION)
        logger.info("Используется StringSession из переменной окружения")
    else:
        session = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "parser_session"
        )
        logger.info("Используется файловая сессия: %s", session)
    client = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    me = await client.get_me()
    logger.info("✅ Telethon подключён как: %s (id=%d)", me.first_name, me.id)

    # Резолв каналов
    channel_ids = set()
    for ch in PARSER_CHANNELS:
        try:
            entity = await client.get_entity(ch)
            channel_ids.add(entity.id)
            title = getattr(entity, "title", ch)
            logger.info("  📢 %s → id=%d (%s)", ch, entity.id, title)
        except Exception as exc:
            logger.error("  ❌ Канал не найден %s: %s", ch, exc)

    if not channel_ids:
        logger.error(
            "❌ Ни один канал не найден. "
            "Проверьте PARSER_CHANNELS и подписки аккаунта."
        )
        await client.disconnect()
        return

    logger.info("🎧 Слушаю %d каналов...\n", len(channel_ids))

    # HTTP клиент для OpenRouter + Bot API
    http_client = httpx.AsyncClient()

    # Обработчик новых сообщений
    @client.on(events.NewMessage(chats=list(channel_ids)))
    async def handler(event: events.NewMessage.Event):
        try:
            await process_message(event, client, http_client)
        except Exception as exc:
            logger.exception("Ошибка обработки сообщения: %s", exc)

    # Работать до отключения
    try:
        await client.run_until_disconnected()
    finally:
        await http_client.aclose()
        logger.info("Parser остановлен.")


if __name__ == "__main__":
    asyncio.run(main())

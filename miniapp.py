import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
import database
from catalog_sources import init_catalog, refresh

STATIC = Path(__file__).with_name("miniapp_static")
MOSCOW = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("uvicorn.error")


def init_pilot():
    with database._conn() as connection:
        cursor = connection.cursor()
        init_catalog(cursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_saves (
                user_key TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                PRIMARY KEY (user_key, event_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_actions (
                action_id TEXT PRIMARY KEY,
                user_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                impression_id TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                surface TEXT NOT NULL,
                position INTEGER NOT NULL,
                dwell_ms INTEGER,
                candidate_ids TEXT,
                policy TEXT NOT NULL
            )
        """)


async def sync_catalog():
    while True:
        try:
            await asyncio.to_thread(refresh)
        except Exception:
            logger.exception("Ошибка обновления внешней афиши")
        await asyncio.sleep(6 * 60 * 60)


@asynccontextmanager
async def lifespan(_app):
    init_pilot()
    task = asyncio.create_task(sync_catalog()) if database._USE_PG else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


def viewer(x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data:
        return None
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise HTTPException(503, "Авторизация временно недоступна.")
    try:
        pairs = parse_qsl(x_telegram_init_data, strict_parsing=True)
        data = dict(pairs)
        if len(pairs) != len(data):
            raise ValueError()
        signature = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        age = time.time() - int(data["auth_date"])
        if not hmac.compare_digest(expected, signature) or not -60 <= age <= 86400:
            raise ValueError()
        user_id = json.loads(data["user"])["id"]
        if type(user_id) is not int:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "Открой афишу заново в Telegram.") from exc

    return hmac.new(secret, f"cuda-idti-pilot:{user_id}".encode(), hashlib.sha256).hexdigest()


def signed_viewer(user_key=Depends(viewer)):
    if user_key is None:
        raise HTTPException(401, "Эта функция доступна в Telegram.")
    return user_key


def external_link(link):
    try:
        parts = urlsplit(link or "")
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username:
            return link
    except ValueError:
        pass
    return None


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    database._fetchone("SELECT 1 FROM events LIMIT 1")
    return {"ok": True}


@app.get("/api/feed")
def feed(response: Response, user_key=Depends(viewer)):
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now(MOSCOW).date()
    rows = database._fetch(
        """SELECT id, name, date_display, date_sort, location, link, description,
                  photo_file_id, event_type, topic, status
           FROM events WHERE status IN ('approved', 'miniapp_catalog') AND date_sort BETWEEN ? AND ?
           ORDER BY CASE WHEN status = 'approved' THEN 0 ELSE 1 END, date_sort, id LIMIT 120""",
        (today.isoformat(), (today + timedelta(days=60)).isoformat()),
    )
    saved = database._fetch("SELECT event_id FROM miniapp_saves WHERE user_key = ?", (user_key,)) if user_key else []
    events = []
    for event_id, name, date_display, date_sort, location, link, description, photo, kind, topic, status in rows:
        image = external_link(photo) if status == "miniapp_catalog" else (f"/api/photo/{event_id}" if photo else None)
        events.append({
            "id": event_id, "name": name, "date_display": date_display,
            "date_sort": date_sort, "location": location, "link": external_link(link),
            "description": description, "photo": image,
            "type": kind, "topic": topic,
        })
    events.sort(key=lambda event: event["date_sort"])
    return {"events": events, "saved": [row[0] for row in saved], "authenticated": user_key is not None}


@lru_cache(maxsize=12)
def event_photo(file_id, token):
    api = f"https://api.telegram.org/bot{token}/getFile"
    with httpx.Client(timeout=7) as client:
        metadata = client.post(api, json={"file_id": file_id})
        metadata.raise_for_status()
        path = metadata.json()["result"]["file_path"]
        with client.stream("GET", f"https://api.telegram.org/file/bot{token}/{path}") as image:
            image.raise_for_status()
            content = bytearray()
            for part in image.iter_bytes():
                content.extend(part)
                if len(content) > 6_000_000:
                    raise ValueError("Photo too large")
    if not content.startswith(b"\xff\xd8"):
        raise ValueError("Expected a Telegram JPEG photo")
    return bytes(content)


@app.get("/api/photo/{event_id}")
def photo(event_id: int):
    row = database._fetchone(
        "SELECT photo_file_id FROM events WHERE id = ? AND status = 'approved'", (event_id,)
    )
    token = os.environ.get("BOT_TOKEN")
    if not row or not row[0] or not token:
        raise HTTPException(404)
    try:
        content = event_photo(row[0], token)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(502, "Фото недоступно.") from exc
    return Response(content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})


class Action(BaseModel):
    action_id: UUID
    impression_id: UUID
    session_id: UUID
    event_id: int = Field(gt=0)
    kind: Literal["impression", "open", "view_end", "save", "unsave", "outbound_click"]
    occurred_at: datetime
    surface: Literal["discover", "saved"]
    position: int = Field(ge=0, lt=120)
    dwell_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    candidate_ids: list[int] | None = Field(default=None, max_length=120)


@app.post("/api/actions")
def record(actions: list[Action], user_key=Depends(signed_viewer)):
    if not 1 <= len(actions) <= 40:
        raise HTTPException(422, "Некорректный пакет действий.")

    now = datetime.now(timezone.utc).isoformat()
    accepted = []
    with database._conn() as connection:
        cursor = connection.cursor()
        for action in actions:
            if action.occurred_at.tzinfo is None:
                raise HTTPException(422, "Время действия должно содержать часовой пояс.")
            def execute(query, params):
                return cursor.execute(database._ph(query), params)

            execute("SELECT 1 FROM events WHERE id = ? AND status IN ('approved', 'miniapp_catalog')", (action.event_id,))
            if not cursor.fetchone():
                continue
            if action.kind != "impression":
                execute(
                    """SELECT 1 FROM miniapp_actions
                       WHERE action_id = ? AND user_key = ? AND session_id = ?
                         AND event_id = ? AND kind = 'impression'""",
                    (str(action.impression_id), user_key, str(action.session_id), action.event_id),
                )
                if not cursor.fetchone():
                    continue
            elif action.action_id != action.impression_id:
                raise HTTPException(422, "Некорректный идентификатор показа.")
            execute(
                """INSERT INTO miniapp_actions
                   (action_id, user_key, session_id, impression_id, event_id, kind,
                    occurred_at, received_at, surface, position, dwell_ms, candidate_ids, policy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (action_id) DO NOTHING""",
                (str(action.action_id), user_key, str(action.session_id), str(action.impression_id),
                 action.event_id, action.kind, action.occurred_at.astimezone(timezone.utc).isoformat(),
                 now, action.surface, action.position, action.dwell_ms,
                 json.dumps(action.candidate_ids) if action.kind == "impression" else None, "date_v1"),
            )
            accepted.append(str(action.action_id))
            if not cursor.rowcount:
                continue
            if action.kind == "save":
                execute(
                    "INSERT INTO miniapp_saves VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (user_key, action.event_id),
                )
            elif action.kind == "unsave":
                execute("DELETE FROM miniapp_saves WHERE user_key = ? AND event_id = ?", (user_key, action.event_id))
    return {"accepted": accepted}

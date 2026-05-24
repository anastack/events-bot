import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── backend detection ─────────────────────────────────────────────────────────

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Railway/Heroku give "postgres://", psycopg2 needs "postgresql://"
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = "postgresql://" + _DATABASE_URL[len("postgres://"):]

_USE_PG = _DATABASE_URL.startswith("postgresql://")

if _USE_PG:
    import psycopg2
else:
    DB_PATH = os.environ.get(
        "DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.db"),
    )


def _ph(sql: str) -> str:
    """Replace ? placeholders with %s for PostgreSQL."""
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
        with sqlite3.connect(DB_PATH) as c:
            yield c


# ── init ──────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id            SERIAL PRIMARY KEY,
                    name          TEXT   NOT NULL,
                    date_display  TEXT   NOT NULL,
                    date_sort     TEXT   NOT NULL,
                    location      TEXT   NOT NULL,
                    link          TEXT,
                    description   TEXT,
                    photo_file_id TEXT,
                    user_id       BIGINT NOT NULL,
                    created_at    TEXT   NOT NULL,
                    event_type    TEXT,
                    topic         TEXT,
                    status        TEXT   NOT NULL DEFAULT 'approved'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attendees (
                    event_id   INTEGER NOT NULL,
                    user_id    BIGINT  NOT NULL,
                    username   TEXT,
                    first_name TEXT    NOT NULL,
                    anonymous  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (event_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    event_id      INTEGER NOT NULL,
                    user_id       BIGINT  NOT NULL,
                    reminder_type TEXT    NOT NULL,
                    PRIMARY KEY (event_id, user_id, reminder_type)
                )
            """)
            for col, ddl in [
                ("description",  "TEXT"),
                ("photo_file_id","TEXT"),
                ("event_type",   "TEXT"),
                ("topic",        "TEXT"),
                ("status",       "TEXT NOT NULL DEFAULT 'approved'"),
            ]:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='events' AND column_name=%s",
                    (col,),
                )
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='attendees' AND column_name='anonymous'"
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE attendees ADD COLUMN anonymous INTEGER NOT NULL DEFAULT 0"
                )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_topics (
                    user_id BIGINT NOT NULL,
                    topic   TEXT   NOT NULL,
                    PRIMARY KEY (user_id, topic)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_notifications (
                    user_id BIGINT  PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    name          TEXT    NOT NULL,
                    date_display  TEXT    NOT NULL,
                    date_sort     TEXT    NOT NULL,
                    location      TEXT    NOT NULL,
                    link          TEXT,
                    description   TEXT,
                    photo_file_id TEXT,
                    user_id       INTEGER NOT NULL,
                    created_at    TEXT    NOT NULL,
                    event_type    TEXT,
                    topic         TEXT,
                    status        TEXT    NOT NULL DEFAULT 'approved'
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS attendees (
                    event_id   INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    username   TEXT,
                    first_name TEXT    NOT NULL,
                    anonymous  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (event_id, user_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    event_id      INTEGER NOT NULL,
                    user_id       INTEGER NOT NULL,
                    reminder_type TEXT    NOT NULL,
                    PRIMARY KEY (event_id, user_id, reminder_type)
                )
            """)
            existing_events = {row[1] for row in c.execute("PRAGMA table_info(events)")}
            for col, ddl in [
                ("description",  "TEXT"),
                ("photo_file_id","TEXT"),
                ("event_type",   "TEXT"),
                ("topic",        "TEXT"),
                ("status",       "TEXT NOT NULL DEFAULT 'approved'"),
            ]:
                if col not in existing_events:
                    c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
            existing_att = {row[1] for row in c.execute("PRAGMA table_info(attendees)")}
            if "anonymous" not in existing_att:
                c.execute("ALTER TABLE attendees ADD COLUMN anonymous INTEGER NOT NULL DEFAULT 0")
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_topics (
                    user_id INTEGER NOT NULL,
                    topic   TEXT    NOT NULL,
                    PRIMARY KEY (user_id, topic)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_notifications (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
            """)

    logger.info(
        "DB ready. Backend: %s",
        f"PostgreSQL ({_DATABASE_URL[:30]}...)" if _USE_PG else f"SQLite ({DB_PATH})",
    )


# ── write ─────────────────────────────────────────────────────────────────────

def add_event(
    name: str,
    date_display: str,
    date_sort: str,
    location: str,
    link: Optional[str],
    description: Optional[str],
    photo_file_id: Optional[str],
    user_id: int,
    event_type: Optional[str] = None,
    topic: Optional[str] = None,
    status: str = "approved",
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
            return cur.lastrowid  # type: ignore[return-value]


def add_attendee(
    event_id: int, user_id: int, username: Optional[str],
    first_name: str, anonymous: bool = False,
) -> None:
    anon = 1 if anonymous else 0
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute("""
                INSERT INTO attendees (event_id, user_id, username, first_name, anonymous)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (event_id, user_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        anonymous = EXCLUDED.anonymous
            """, (event_id, user_id, username, first_name, anon))
        else:
            c.execute(
                "INSERT OR REPLACE INTO attendees "
                "(event_id, user_id, username, first_name, anonymous) VALUES (?, ?, ?, ?, ?)",
                (event_id, user_id, username, first_name, anon),
            )


def remove_attendee(event_id: int, user_id: int) -> None:
    sql = _ph("DELETE FROM attendees WHERE event_id = ? AND user_id = ?")
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute(sql, (event_id, user_id))
        else:
            c.execute(sql, (event_id, user_id))


def update_event_field(event_id: int, field: str, value: object) -> None:
    _ALLOWED = {
        "name", "date_display", "date_sort", "location", "link",
        "description", "photo_file_id", "event_type", "topic", "status",
    }
    if field not in _ALLOWED:
        raise ValueError(f"Unknown field: {field}")
    sql = _ph(f"UPDATE events SET {field} = ? WHERE id = ?")
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute(sql, (value, event_id))
        else:
            c.execute(sql, (value, event_id))


def delete_event(event_id: int) -> None:
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute("DELETE FROM attendees WHERE event_id = %s", (event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        else:
            c.execute("DELETE FROM attendees WHERE event_id = ?", (event_id,))
            c.execute("DELETE FROM events WHERE id = ?", (event_id,))


# ── read ──────────────────────────────────────────────────────────────────────

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


def get_upcoming_events() -> List[Tuple]:
    today = date.today()
    two_weeks = today + timedelta(weeks=2)
    return _fetch(
        """
        SELECT id, name, date_display, location, link, description,
               photo_file_id, event_type, topic
        FROM events
        WHERE date_sort >= ? AND date_sort <= ? AND status = 'approved'
        ORDER BY date_sort ASC
        LIMIT 10
        """,
        (today.isoformat(), two_weeks.isoformat()),
    )


def get_event_by_id(event_id: int) -> Optional[Tuple]:
    return _fetchone(
        """
        SELECT id, name, date_display, location, link, description,
               photo_file_id, event_type, topic
        FROM events WHERE id = ?
        """,
        (event_id,),
    )


def get_event_submitter_id(event_id: int) -> Optional[int]:
    row = _fetchone("SELECT user_id FROM events WHERE id = ?", (event_id,))
    return row[0] if row else None


def get_attendees(event_id: int) -> List[Tuple]:
    """Visible (non-anonymous) attendees for the public list."""
    return _fetch(
        "SELECT user_id, username, first_name FROM attendees "
        "WHERE event_id = ? AND anonymous = 0",
        (event_id,),
    )


def get_all_attendees(event_id: int) -> List[Tuple]:
    """All attendees including anonymous — used for reminders."""
    return _fetch(
        "SELECT user_id, username, first_name FROM attendees WHERE event_id = ?",
        (event_id,),
    )


def is_attending(event_id: int, user_id: int) -> bool:
    return _fetchone(
        "SELECT 1 FROM attendees WHERE event_id = ? AND user_id = ?",
        (event_id, user_id),
    ) is not None


def get_attendee_count(event_id: int) -> int:
    row = _fetchone("SELECT COUNT(*) FROM attendees WHERE event_id = ?", (event_id,))
    return row[0] if row else 0


def get_all_events() -> List[Tuple]:
    return _fetch(
        "SELECT id, name, date_display, date_sort FROM events ORDER BY date_sort ASC"
    )


# ── filter & search ───────────────────────────────────────────────────────────

def get_events_filtered(event_type: Optional[str] = None, topic: Optional[str] = None) -> List[Tuple]:
    today = date.today().isoformat()
    conditions = ["date_sort >= ?", "status = 'approved'"]
    params: list = [today]
    if event_type is not None:
        conditions.append("event_type = ?")
        params.append(event_type)
    if topic is not None:
        conditions.append("topic = ?")
        params.append(topic)
    sql = (
        "SELECT id, name, date_display, location, link, description, "
        "photo_file_id, event_type, topic FROM events "
        f"WHERE {' AND '.join(conditions)} ORDER BY date_sort ASC"
    )
    return _fetch(sql, tuple(params))


def get_distinct_event_types() -> List[str]:
    today = date.today().isoformat()
    rows = _fetch(
        "SELECT DISTINCT event_type FROM events "
        "WHERE date_sort >= ? AND status = 'approved' AND event_type IS NOT NULL "
        "ORDER BY event_type",
        (today,),
    )
    return [r[0] for r in rows]


def get_distinct_topics() -> List[str]:
    today = date.today().isoformat()
    rows = _fetch(
        "SELECT DISTINCT topic FROM events "
        "WHERE date_sort >= ? AND status = 'approved' AND topic IS NOT NULL "
        "ORDER BY topic",
        (today,),
    )
    return [r[0] for r in rows]


def get_user_events(user_id: int) -> List[Tuple]:
    today = date.today().isoformat()
    return _fetch(
        "SELECT e.id, e.name, e.date_display, e.location, e.link, e.description, "
        "e.photo_file_id, e.event_type, e.topic "
        "FROM events e JOIN attendees a ON a.event_id = e.id "
        "WHERE a.user_id = ? AND e.date_sort >= ? AND e.status = 'approved' "
        "ORDER BY e.date_sort ASC",
        (user_id, today),
    )


def get_events_on_date(date_str: str) -> List[Tuple]:
    return _fetch(
        "SELECT id, name, date_display, date_sort FROM events "
        "WHERE date_sort = ? AND status = 'approved'",
        (date_str,),
    )


# ── reminders ─────────────────────────────────────────────────────────────────

def is_reminder_sent(event_id: int, user_id: int, reminder_type: str) -> bool:
    return _fetchone(
        "SELECT 1 FROM reminders WHERE event_id = ? AND user_id = ? AND reminder_type = ?",
        (event_id, user_id, reminder_type),
    ) is not None


def mark_reminder_sent(event_id: int, user_id: int, reminder_type: str) -> None:
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute(
                "INSERT INTO reminders (event_id, user_id, reminder_type) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (event_id, user_id, reminder_type),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO reminders (event_id, user_id, reminder_type) "
                "VALUES (?, ?, ?)",
                (event_id, user_id, reminder_type),
            )


# ── user topics & notifications ───────────────────────────────────────────────

def get_user_topics(user_id: int) -> List[str]:
    rows = _fetch("SELECT topic FROM user_topics WHERE user_id = ?", (user_id,))
    return [r[0] for r in rows]


def set_user_topics(user_id: int, topics: List[str]) -> None:
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute("DELETE FROM user_topics WHERE user_id = %s", (user_id,))
            for t in topics:
                cur.execute(
                    "INSERT INTO user_topics (user_id, topic) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, t),
                )
        else:
            c.execute("DELETE FROM user_topics WHERE user_id = ?", (user_id,))
            for t in topics:
                c.execute(
                    "INSERT OR IGNORE INTO user_topics (user_id, topic) VALUES (?, ?)",
                    (user_id, t),
                )


def get_notification_pref(user_id: int) -> Optional[bool]:
    row = _fetchone("SELECT enabled FROM user_notifications WHERE user_id = ?", (user_id,))
    return None if row is None else bool(row[0])


def set_notification_pref(user_id: int, enabled: bool) -> None:
    val = 1 if enabled else 0
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute(
                "INSERT INTO user_notifications (user_id, enabled) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled",
                (user_id, val),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO user_notifications (user_id, enabled) VALUES (?, ?)",
                (user_id, val),
            )


def get_users_subscribed_to_topic(topic: str) -> List[int]:
    rows = _fetch(
        "SELECT ut.user_id FROM user_topics ut "
        "JOIN user_notifications un ON un.user_id = ut.user_id "
        "WHERE ut.topic = ? AND un.enabled = 1",
        (topic,),
    )
    return [r[0] for r in rows]

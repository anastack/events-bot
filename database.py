import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
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
                    PRIMARY KEY (event_id, user_id)
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
                    PRIMARY KEY (event_id, user_id)
                )
            """)
            existing = {row[1] for row in c.execute("PRAGMA table_info(events)")}
            for col, ddl in [
                ("description",  "TEXT"),
                ("photo_file_id","TEXT"),
                ("event_type",   "TEXT"),
                ("topic",        "TEXT"),
                ("status",       "TEXT NOT NULL DEFAULT 'approved'"),
            ]:
                if col not in existing:
                    c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")

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


def add_attendee(event_id: int, user_id: int, username: Optional[str], first_name: str) -> None:
    with _conn() as c:
        if _USE_PG:
            c.cursor().execute("""
                INSERT INTO attendees (event_id, user_id, username, first_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id, user_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name
            """, (event_id, user_id, username, first_name))
        else:
            c.execute(
                "INSERT OR REPLACE INTO attendees "
                "(event_id, user_id, username, first_name) VALUES (?, ?, ?, ?)",
                (event_id, user_id, username, first_name),
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
    return _fetch(
        """
        SELECT id, name, date_display, location, link, description,
               photo_file_id, event_type, topic
        FROM events
        WHERE date_sort >= ? AND status = 'approved'
        ORDER BY date_sort ASC
        """,
        (date.today().isoformat(),),
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

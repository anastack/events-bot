import sqlite3
from datetime import date, datetime
from typing import List, Optional, Tuple

DB_PATH = "events.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendees (
                event_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                first_name TEXT    NOT NULL,
                PRIMARY KEY (event_id, user_id)
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        for col, ddl in [
            ("description",  "TEXT"),
            ("photo_file_id","TEXT"),
            ("event_type",   "TEXT"),
            ("topic",        "TEXT"),
            ("status",       "TEXT NOT NULL DEFAULT 'approved'"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")


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
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO events
                (name, date_display, date_sort, location, link, description,
                 photo_file_id, user_id, created_at, event_type, topic, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, date_display, date_sort, location, link, description,
             photo_file_id, user_id, datetime.now().isoformat(),
             event_type, topic, status),
        )
        return cur.lastrowid  # type: ignore[return-value]


def get_upcoming_events() -> List[Tuple]:
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT id, name, date_display, location, link, description,
                   photo_file_id, event_type, topic
            FROM events
            WHERE date_sort >= ? AND status = 'approved'
            ORDER BY date_sort ASC
            """,
            (today,),
        )
        return cur.fetchall()


def get_event_by_id(event_id: int) -> Optional[Tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT id, name, date_display, location, link, description,
                   photo_file_id, event_type, topic
            FROM events WHERE id = ?
            """,
            (event_id,),
        )
        return cur.fetchone()


def get_event_submitter_id(event_id: int) -> Optional[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT user_id FROM events WHERE id = ?", (event_id,))
        row = cur.fetchone()
        return row[0] if row else None


def add_attendee(event_id: int, user_id: int, username: Optional[str], first_name: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO attendees (event_id, user_id, username, first_name) VALUES (?, ?, ?, ?)",
            (event_id, user_id, username, first_name),
        )


def remove_attendee(event_id: int, user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM attendees WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        )


def get_attendees(event_id: int) -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT user_id, username, first_name FROM attendees WHERE event_id = ? ORDER BY rowid",
            (event_id,),
        )
        return cur.fetchall()


def is_attending(event_id: int, user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT 1 FROM attendees WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        )
        return cur.fetchone() is not None


def get_attendee_count(event_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM attendees WHERE event_id = ?", (event_id,)
        )
        return cur.fetchone()[0]


def get_all_events() -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, name, date_display, date_sort FROM events ORDER BY date_sort ASC"
        )
        return cur.fetchall()


_ALLOWED_FIELDS = {
    "name", "date_display", "date_sort", "location", "link",
    "description", "photo_file_id", "event_type", "topic", "status",
}


def update_event_field(event_id: int, field: str, value: object) -> None:
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"Unknown field: {field}")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE events SET {field} = ? WHERE id = ?", (value, event_id))


def delete_event(event_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM attendees WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))

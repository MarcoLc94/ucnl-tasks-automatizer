"""
SQLite compartido por usuarios, sesiones y tareas (data/app.db).
"""
import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent.parent / "data"))
_DB_PATH = DATA_DIR / "app.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    ucnl_username      TEXT NOT NULL DEFAULT '',
    ucnl_password_enc  TEXT NOT NULL DEFAULT '',
    career             TEXT NOT NULL DEFAULT 'Ingeniería en Desarrollo de Software',
    scan_hour          INTEGER NOT NULL DEFAULT 8,
    scan_minute        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_name       TEXT NOT NULL,
    task_title        TEXT NOT NULL,
    task_description  TEXT NOT NULL,
    task_type         TEXT NOT NULL,
    task_url          TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    due_date          TEXT,
    available_from    TEXT,
    ai_response       TEXT,
    exam_questions    TEXT NOT NULL DEFAULT '[]',
    output_format     TEXT NOT NULL DEFAULT 'docx',
    opens_at          TEXT,
    closes_at         TEXT,
    course_grade      TEXT,
    teacher           TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(_DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA foreign_keys = ON")
            _conn.execute("PRAGMA journal_mode = WAL")
            _conn.executescript(_SCHEMA)
            _migrate(_conn)
        return _conn


def _migrate(c: sqlite3.Connection) -> None:
    """Agrega columnas nuevas a bases creadas con versiones anteriores."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
    for col in ("opens_at", "closes_at", "course_grade", "teacher"):
        if col not in cols:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "career" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN career TEXT NOT NULL DEFAULT 'Ingeniería en Desarrollo de Software'")


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()

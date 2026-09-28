from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .security import decrypt, encrypt, hash_password, new_token, token_hash, verify_password

SESSION_DAYS = 30


@dataclass
class User:
    id: int
    username: str
    display_name: str
    career: str
    ucnl_username: str
    ucnl_password_enc: str
    scan_hour: int
    scan_minute: int

    @property
    def ucnl_password(self) -> str:
        return decrypt(self.ucnl_password_enc)

    @property
    def has_ucnl_credentials(self) -> bool:
        return bool(self.ucnl_username and self.ucnl_password_enc)

    @property
    def data_dir(self) -> Path:
        path = db.DATA_DIR / "users" / str(self.id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def auth_state_path(self) -> Path:
        return self.data_dir / "auth_state.json"

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"


def _from_row(row) -> User | None:
    if not row:
        return None
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        career=row["career"],
        ucnl_username=row["ucnl_username"],
        ucnl_password_enc=row["ucnl_password_enc"],
        scan_hour=row["scan_hour"],
        scan_minute=row["scan_minute"],
    )


def create_user(username: str, password: str, display_name: str, scan_hour: int = 8, scan_minute: int = 0) -> User:
    cur = db.execute(
        "INSERT INTO users (username, password_hash, display_name, scan_hour, scan_minute, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username.strip().lower(), hash_password(password), display_name, scan_hour, scan_minute,
         datetime.now().isoformat()),
    )
    return get_user(cur.lastrowid)


def get_user(user_id: int) -> User | None:
    return _from_row(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_by_username(username: str) -> User | None:
    return _from_row(db.query_one("SELECT * FROM users WHERE username = ?", (username.strip().lower(),)))


def list_users() -> list[User]:
    return [_from_row(r) for r in db.query("SELECT * FROM users ORDER BY id")]


def delete_user(user_id: int) -> None:
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def authenticate(username: str, password: str) -> User | None:
    row = db.query_one("SELECT * FROM users WHERE username = ?", (username.strip().lower(),))
    if row and verify_password(password, row["password_hash"]):
        return _from_row(row)
    return None


def set_password(user_id: int, password: str) -> None:
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def update_profile(
    user_id: int,
    display_name: str | None = None,
    career: str | None = None,
    ucnl_username: str | None = None,
    ucnl_password: str | None = None,
    scan_hour: int | None = None,
    scan_minute: int | None = None,
) -> User:
    fields = {
        "display_name": display_name,
        "career": career,
        "ucnl_username": ucnl_username,
        "ucnl_password_enc": encrypt(ucnl_password) if ucnl_password else None,
        "scan_hour": scan_hour,
        "scan_minute": scan_minute,
    }
    updates = {k: v for k, v in fields.items() if v is not None}
    if updates:
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        db.execute(f"UPDATE users SET {sets} WHERE id = :id", {**updates, "id": user_id})
    if ucnl_username is not None or ucnl_password:
        # Credenciales nuevas → descartar la sesión guardada de UCNL
        user = get_user(user_id)
        user.auth_state_path.unlink(missing_ok=True)
    return get_user(user_id)


# ─── Sesiones del panel ───────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    token = new_token()
    expires = datetime.now() + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash(token), user_id, expires.isoformat()),
    )
    return token


def user_from_session(token: str | None) -> User | None:
    if not token:
        return None
    row = db.query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash(token), datetime.now().isoformat()),
    )
    return _from_row(row)


def delete_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

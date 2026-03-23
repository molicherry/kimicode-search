import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from auth import hash_api_key, hash_password, mask_secret, mask_visible_ends


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cursor_lastrowid(cursor: sqlite3.Cursor) -> int:
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise ValueError("数据库未返回 lastrowid。")
    return int(lastrowid)


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    raw_key TEXT NOT NULL DEFAULT '',
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    search_rpm INTEGER NOT NULL DEFAULT 60,
                    fetch_rpm INTEGER NOT NULL DEFAULT 60,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS kimi_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    masked_key TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    user_id INTEGER,
                    user_api_key_id INTEGER,
                    endpoint TEXT NOT NULL,
                    selected_kimi_key_id INTEGER,
                    status_code INTEGER,
                    success INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    client_ip TEXT,
                    request_summary TEXT NOT NULL,
                    response_summary TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(user_api_key_id) REFERENCES user_api_keys(id) ON DELETE SET NULL,
                    FOREIGN KEY(selected_kimi_key_id) REFERENCES kimi_api_keys(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS rate_limit_counters (
                    user_api_key_id INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_api_key_id, endpoint, window_start),
                    FOREIGN KEY(user_api_key_id) REFERENCES user_api_keys(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_id ON user_api_keys(user_id);
                CREATE INDEX IF NOT EXISTS idx_request_logs_user_id ON request_logs(user_id);
                CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at);
                """
            )

    def bootstrap_admin(self, username: str, password: str) -> None:
        if self.get_user_by_username(username):
            return
        self.create_user(username=username, password_hash=hash_password(password), role="admin")

    def get_user_by_username(self, username: str):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def _paginate_query(self, base_query: str, params: tuple, page: int, page_size: int):
        safe_page = max(page, 1)
        safe_page_size = max(page_size, 1)
        count_query = f"SELECT COUNT(*) AS value FROM ({base_query}) AS subquery"
        offset = (safe_page - 1) * safe_page_size
        paged_query = f"{base_query} LIMIT ? OFFSET ?"
        with self.connect() as connection:
            total = int(connection.execute(count_query, params).fetchone()["value"])
            rows = connection.execute(paged_query, params + (safe_page_size, offset)).fetchall()
        total_pages = max((total + safe_page_size - 1) // safe_page_size, 1)
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": min(safe_page, total_pages),
            "page_size": safe_page_size,
            "total_pages": total_pages,
        }

    def list_users(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_users_paginated(
        self,
        search: str = "",
        role: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 10,
    ):
        conditions: list[str] = []
        params: list[object] = []
        if search:
            conditions.append("username LIKE ?")
            params.append(f"%{search}%")
        if role in {"admin", "user"}:
            conditions.append("role = ?")
            params.append(role)
        if status == "active":
            conditions.append("is_active = 1")
        elif status == "disabled":
            conditions.append("is_active = 0")

        query = "SELECT * FROM users"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC"
        return self._paginate_query(query, tuple(params), page, page_size)

    def create_user(self, username: str, password_hash: str, role: str = "user") -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (username, password_hash, role, now, now),
            )
            return cursor_lastrowid(cursor)

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )

    def set_user_active(self, user_id: int, is_active: bool) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if is_active else 0, now, user_id),
            )

    def touch_last_login(self, user_id: int) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_id),
            )

    def create_session(
        self,
        session_id: str,
        user_id: int,
        expires_at: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (id, user_id, expires_at, created_at, last_seen_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, expires_at, now, now, ip_address, user_agent),
            )

    def get_session(self, session_id: str):
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT sessions.id AS session_id, sessions.expires_at, users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.id = ? AND users.is_active = 1
                """,
                (session_id,),
            ).fetchone()

            if row is None:
                return None

            if row["expires_at"] <= now:
                connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                return None

            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (now, session_id),
            )
        return dict(row)

    def delete_session(self, session_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def create_user_api_key(
        self,
        user_id: int,
        name: str,
        raw_key: str,
        search_rpm: int,
        fetch_rpm: int,
        expires_at: str | None,
    ) -> int:
        now = utc_now_iso()
        key_hash = hash_api_key(raw_key)
        key_prefix = f"kimu_{mask_visible_ends(raw_key[5:])}"
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO user_api_keys (
                    user_id, name, raw_key, key_hash, key_prefix, is_active, search_rpm, fetch_rpm,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (user_id, name, raw_key, key_hash, key_prefix, search_rpm, fetch_rpm, expires_at, now, now),
            )
            return cursor_lastrowid(cursor)

    def list_user_api_keys(self, user_id: int | None = None):
        query = (
            "SELECT user_api_keys.*, users.username FROM user_api_keys "
            "JOIN users ON users.id = user_api_keys.user_id"
        )
        params: tuple = ()
        if user_id is not None:
            query += " WHERE user_api_keys.user_id = ?"
            params = (user_id,)
        query += " ORDER BY user_api_keys.id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_user_api_keys_paginated(
        self,
        user_id: int | None = None,
        search: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 10,
    ):
        conditions: list[str] = []
        params: list[object] = []
        query = (
            "SELECT user_api_keys.*, users.username FROM user_api_keys "
            "JOIN users ON users.id = user_api_keys.user_id"
        )
        if user_id is not None:
            conditions.append("user_api_keys.user_id = ?")
            params.append(user_id)
        if search:
            conditions.append("(user_api_keys.name LIKE ? OR user_api_keys.key_prefix LIKE ? OR users.username LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        if status == "active":
            conditions.append("user_api_keys.is_active = 1")
        elif status == "disabled":
            conditions.append("user_api_keys.is_active = 0")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY user_api_keys.id DESC"
        return self._paginate_query(query, tuple(params), page, page_size)

    def get_user_api_key(self, key_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_user_api_key(self, key_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM user_api_keys WHERE id = ?", (key_id,))

    def set_user_api_key_active(self, key_id: int, is_active: bool) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE user_api_keys SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if is_active else 0, now, key_id),
            )

    def validate_user_api_key(self, raw_key: str):
        now = utc_now_iso()
        key_hash = hash_api_key(raw_key)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT user_api_keys.*, users.username, users.is_active AS user_is_active, users.role, users.id AS owner_user_id
                FROM user_api_keys
                JOIN users ON users.id = user_api_keys.user_id
                WHERE key_hash = ?
                """,
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if not result["is_active"] or not result["user_is_active"]:
                return None
            expires_at = result.get("expires_at")
            if expires_at and expires_at <= now:
                return None
            connection.execute(
                "UPDATE user_api_keys SET last_used_at = ?, updated_at = ? WHERE id = ?",
                (now, now, result["id"]),
            )
        return result

    def create_kimi_api_key(self, name: str, api_key: str) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO kimi_api_keys (name, api_key, masked_key, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (name, api_key, f"sk-kimi-{mask_visible_ends(api_key.removeprefix('sk-kimi-'))}", now, now),
            )
            return cursor_lastrowid(cursor)

    def list_kimi_api_keys(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM kimi_api_keys ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_kimi_api_keys_paginated(
        self,
        search: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 10,
    ):
        conditions: list[str] = []
        params: list[object] = []
        query = "SELECT kimi_api_keys.* FROM kimi_api_keys"
        if search:
            conditions.append("(kimi_api_keys.name LIKE ? OR kimi_api_keys.masked_key LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if status == "active":
            conditions.append("kimi_api_keys.is_active = 1")
        elif status == "disabled":
            conditions.append("kimi_api_keys.is_active = 0")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY kimi_api_keys.id DESC"
        return self._paginate_query(query, tuple(params), page, page_size)

    def set_kimi_api_key_active(self, kimi_api_key_id: int, is_active: bool) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE kimi_api_keys SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if is_active else 0, now, kimi_api_key_id),
            )

    def delete_kimi_api_key(self, kimi_api_key_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM kimi_api_keys WHERE id = ?", (kimi_api_key_id,))

    def select_upstream_for_user_api_key(self, user_api_key_id: int):
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT kimi_api_keys.id AS kimi_key_id, kimi_api_keys.name AS kimi_key_name,
                       kimi_api_keys.api_key AS api_key
                FROM kimi_api_keys
                WHERE kimi_api_keys.is_active = 1
                ORDER BY kimi_api_keys.id ASC
                """,
            ).fetchall()
        if not rows:
            return None
        row = dict(random.choice(rows))
        return row

    def increment_rate_counter(self, user_api_key_id: int, endpoint: str, window_start: int) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO rate_limit_counters (user_api_key_id, endpoint, window_start, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_api_key_id, endpoint, window_start)
                DO UPDATE SET request_count = request_count + 1
                """,
                (user_api_key_id, endpoint, window_start),
            )
            row = connection.execute(
                "SELECT request_count FROM rate_limit_counters WHERE user_api_key_id = ? AND endpoint = ? AND window_start = ?",
                (user_api_key_id, endpoint, window_start),
            ).fetchone()
        return int(row["request_count"])

    def insert_request_log(
        self,
        request_id: str,
        user_id: int | None,
        user_api_key_id: int | None,
        endpoint: str,
        selected_kimi_key_id: int | None,
        status_code: int,
        success: bool,
        latency_ms: int,
        client_ip: str,
        request_summary: str,
        response_summary: str,
        error_message: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO request_logs (
                    request_id, user_id, user_api_key_id, endpoint,
                    selected_kimi_key_id, status_code, success, latency_ms, client_ip,
                    request_summary, response_summary, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    user_api_key_id,
                    endpoint,
                    selected_kimi_key_id,
                    status_code,
                    1 if success else 0,
                    latency_ms,
                    client_ip,
                    request_summary,
                    response_summary,
                    error_message,
                    utc_now_iso(),
                ),
            )

    def list_request_logs(self, user_id: int | None = None, limit: int = 200):
        query = (
            "SELECT request_logs.*, users.username, user_api_keys.name AS api_key_name "
            "FROM request_logs "
            "LEFT JOIN users ON users.id = request_logs.user_id "
            "LEFT JOIN user_api_keys ON user_api_keys.id = request_logs.user_api_key_id"
        )
        params: tuple = ()
        if user_id is not None:
            query += " WHERE request_logs.user_id = ?"
            params = (user_id,)
        query += " ORDER BY request_logs.id DESC LIMIT ?"
        params += (limit,)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_request_logs_paginated(
        self,
        user_id: int | None = None,
        search: str = "",
        endpoint: str = "",
        success: str = "",
        page: int = 1,
        page_size: int = 20,
    ):
        conditions: list[str] = []
        params: list[object] = []
        query = (
            "SELECT request_logs.*, users.username, user_api_keys.name AS api_key_name "
            "FROM request_logs "
            "LEFT JOIN users ON users.id = request_logs.user_id "
            "LEFT JOIN user_api_keys ON user_api_keys.id = request_logs.user_api_key_id"
        )
        if user_id is not None:
            conditions.append("request_logs.user_id = ?")
            params.append(user_id)
        if search:
            conditions.append(
                "(COALESCE(users.username, '') LIKE ? OR COALESCE(request_logs.request_summary, '') LIKE ? OR COALESCE(request_logs.error_message, '') LIKE ? OR COALESCE(user_api_keys.name, '') LIKE ?)"
            )
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])
        if endpoint in {"search", "fetch"}:
            conditions.append("request_logs.endpoint = ?")
            params.append(endpoint)
        if success == "success":
            conditions.append("request_logs.success = 1")
        elif success == "failed":
            conditions.append("request_logs.success = 0")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY request_logs.id DESC"
        return self._paginate_query(query, tuple(params), page, page_size)

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )

    def get_setting(self, key: str, default: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    def get_usage_summary_for_user(self, user_id: int):
        with self.connect() as connection:
            total_logs = connection.execute(
                "SELECT COUNT(*) AS value FROM request_logs WHERE user_id = ?",
                (user_id,),
            ).fetchone()["value"]
            success_logs = connection.execute(
                "SELECT COUNT(*) AS value FROM request_logs WHERE user_id = ? AND success = 1",
                (user_id,),
            ).fetchone()["value"]
            api_count = connection.execute(
                "SELECT COUNT(*) AS value FROM user_api_keys WHERE user_id = ?",
                (user_id,),
            ).fetchone()["value"]
        return {
            "total_logs": int(total_logs),
            "success_logs": int(success_logs),
            "api_count": int(api_count),
        }

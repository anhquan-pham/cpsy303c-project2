"""
User store abstraction.

Keith's Function App already created dbo.users (see _ensure_schema in
function_app.py) with this schema:

    id             NVARCHAR(64)  PRIMARY KEY
    email          NVARCHAR(256) UNIQUE
    display_name   NVARCHAR(256) NULL
    password_hash  NVARCHAR(512) NULL   -- NULL for OAuth-only accounts
    oauth_provider NVARCHAR(64)  NULL
    oauth_subject  NVARCHAR(256) NULL
    created_at     DATETIME2

SqlUserStore takes a `connect_fn` - pass in function_app.py's own
`_sql_connect` when you merge this in, so auth reuses the exact same
managed-identity connection (with its auto-pause retry logic) that the
insights pipeline already uses. Nothing here opens its own connection
differently.

InMemoryUserStore is still here for quick local testing without touching
SQL at all (e.g. before your Azure AD login has DB permissions sorted out).
"""

import uuid
import threading


class InMemoryUserStore:
    """Thread-safe in-memory store. Data is lost on restart - local dev only."""

    def __init__(self):
        self._by_email = {}
        self._by_id = {}
        self._lock = threading.Lock()

    def get_by_email(self, email: str) -> dict | None:
        return self._by_email.get(email.lower())

    def get_by_oauth(self, provider: str, oauth_id: str) -> dict | None:
        for user in self._by_id.values():
            if user.get("oauth_provider") == provider and user.get("oauth_id") == oauth_id:
                return user
        return None

    def create_user(self, name: str, email: str, password_hash: str | None = None,
                     oauth_provider: str | None = None, oauth_id: str | None = None) -> dict:
        email = email.lower()
        with self._lock:
            if email in self._by_email:
                raise ValueError("A user with that email already exists")
            user = {
                "id": str(uuid.uuid4()),
                "name": name,
                "email": email,
                "password_hash": password_hash,
                "oauth_provider": oauth_provider,
                "oauth_id": oauth_id,
            }
            self._by_email[email] = user
            self._by_id[user["id"]] = user
            return user


class SqlUserStore:
    """Reads/writes dbo.users in Keith's Azure SQL database (dietdb)."""

    def __init__(self, connect_fn):
        # connect_fn is Keith's _sql_connect - a zero-arg callable returning
        # an open pyodbc connection, with the auto-pause retry already inside it.
        self._connect = connect_fn

    @staticmethod
    def _row_to_user(row) -> dict:
        return {
            "id": row.id,
            "name": row.display_name,
            "email": row.email,
            "password_hash": row.password_hash,
            "oauth_provider": row.oauth_provider,
            "oauth_id": row.oauth_subject,
        }

    def get_by_email(self, email: str) -> dict | None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, email, display_name, password_hash, oauth_provider, oauth_subject "
                "FROM dbo.users WHERE email = ?",
                email.lower(),
            )
            row = cur.fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def get_by_oauth(self, provider: str, oauth_id: str) -> dict | None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, email, display_name, password_hash, oauth_provider, oauth_subject "
                "FROM dbo.users WHERE oauth_provider = ? AND oauth_subject = ?",
                provider, oauth_id,
            )
            row = cur.fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def create_user(self, name: str, email: str, password_hash: str | None = None,
                     oauth_provider: str | None = None, oauth_id: str | None = None) -> dict:
        email = email.lower()
        user_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO dbo.users (id, email, display_name, password_hash, "
                    "oauth_provider, oauth_subject) VALUES (?, ?, ?, ?, ?, ?)",
                    user_id, email, name, password_hash, oauth_provider, oauth_id,
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                # UNIQUE constraint on email fires here for a duplicate signup
                if "UNIQUE" in str(exc) or "duplicate" in str(exc).lower():
                    raise ValueError("A user with that email already exists") from exc
                raise
        finally:
            conn.close()

        return {
            "id": user_id, "name": name, "email": email,
            "password_hash": password_hash,
            "oauth_provider": oauth_provider, "oauth_id": oauth_id,
        }


_store_instance = None


def get_user_store(connect_fn=None):
    """
    Single shared store instance for the Function App process.

    Pass Keith's _sql_connect the first time you call this (e.g. from your
    route handlers in function_app.py) to use dbo.users. Call it with no
    argument to get an in-memory store for quick local testing.
    """
    global _store_instance
    if _store_instance is not None:
        return _store_instance

    _store_instance = SqlUserStore(connect_fn) if connect_fn else InMemoryUserStore()
    return _store_instance
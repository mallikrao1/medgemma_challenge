import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings


class AuthService:
    """Simple SQLite-backed auth, RBAC, and deployment audit service."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = Path(db_path).expanduser()
        else:
            base = Path(__file__).resolve().parent.parent
            self.db_path = base / "data" / "platform.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_duration_hours = max(1, int(getattr(settings, "SESSION_DURATION_HOURS", 24) or 24))
        self.session_sliding_refresh = bool(getattr(settings, "SESSION_SLIDING_REFRESH", True))
        self.session_refresh_window_minutes = max(
            1, int(getattr(settings, "SESSION_REFRESH_WINDOW_MINUTES", 180) or 180)
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    created_by INTEGER,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    service TEXT NOT NULL,
                    can_read INTEGER NOT NULL DEFAULT 0,
                    can_write INTEGER NOT NULL DEFAULT 0,
                    can_execute INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, service),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    is_revoked INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT,
                    resource_type TEXT,
                    resource_name TEXT,
                    region TEXT,
                    environment TEXT,
                    status TEXT NOT NULL,
                    request_text TEXT,
                    execution_summary TEXT,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remediation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT UNIQUE NOT NULL,
                    request_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    request_snapshot_json TEXT,
                    resume_context_json TEXT,
                    required_permissions_json TEXT,
                    result_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    approval_scope TEXT NOT NULL DEFAULT 'request_run',
                    expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    note TEXT,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.commit()

        # Bootstrap default admin only when no users exist.
        if not self._find_user_by_username("admin"):
            self.create_user(
                admin_user_id=None,
                username="admin",
                password="admin",
                role="admin",
                permissions=[{"service": "all", "can_read": True, "can_write": True, "can_execute": True}],
            )

    def _hash_password(self, password: str, salt: Optional[str] = None) -> str:
        rounds = 210000
        salt = salt or secrets.token_hex(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds).hex()
        return f"pbkdf2_sha256${rounds}${salt}${derived}"

    def _verify_password(self, password: str, encoded: str) -> bool:
        try:
            scheme, rounds, salt, digest = encoded.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            test = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                int(rounds),
            ).hex()
            return secrets.compare_digest(test, digest)
        except Exception:
            return False

    def _now(self) -> int:
        return int(time.time())

    def _ts_to_iso(self, ts: Optional[int]) -> Optional[str]:
        if ts is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))

    def _normalize_user_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "created_at": self._ts_to_iso(row["created_at"]),
            "updated_at": self._ts_to_iso(row["updated_at"]),
        }

    def _normalize_permission_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "service": row["service"],
            "can_read": bool(row["can_read"]),
            "can_write": bool(row["can_write"]),
            "can_execute": bool(row["can_execute"]),
        }

    def _normalize_resource(self, resource_type: str) -> str:
        raw = (resource_type or "").strip().lower()
        alias = {
            "spark": "emr",
            "database": "rds",
            "securitygroup": "security_group",
            "natgateway": "nat_gateway",
            "internetgateway": "internet_gateway",
            "logs": "log_group",
        }
        return alias.get(raw, raw)

    def _json_dump(self, payload: Any) -> Optional[str]:
        if payload is None:
            return None
        try:
            return json.dumps(payload, ensure_ascii=True)
        except Exception:
            try:
                return json.dumps({"raw": str(payload)}, ensure_ascii=True)
            except Exception:
                return None

    def _json_load(self, raw: Optional[str]) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}

    def _find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? LIMIT 1",
                (username,),
            ).fetchone()
            if not row:
                return None
            user = dict(self._normalize_user_row(row))
            user["password_hash"] = row["password_hash"]
            return user

    def _find_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            user = dict(self._normalize_user_row(row))
            user["password_hash"] = row["password_hash"]
            return user

    def create_session(self, user_id: int, duration_hours: Optional[int] = None) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._now()
        hours = self.session_duration_hours if duration_hours is None else max(1, int(duration_hours))
        exp = now + int(hours * 3600)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, is_revoked) VALUES (?, ?, ?, ?, 0)",
                (token_hash, user_id, now, exp),
            )
            conn.commit()
        return {
            "token": token,
            "expires_at": self._ts_to_iso(exp),
        }

    def authenticate_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.*, s.expires_at AS session_expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.is_revoked = 0
                  AND s.expires_at > ?
                  AND u.is_active = 1
                LIMIT 1
                """,
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            if self.session_sliding_refresh:
                session_exp = int(row["session_expires_at"] or 0)
                remaining = max(0, session_exp - now)
                refresh_window = int(self.session_refresh_window_minutes * 60)
                if remaining <= refresh_window:
                    new_exp = now + int(self.session_duration_hours * 3600)
                    conn.execute(
                        "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                        (new_exp, token_hash),
                    )
                    conn.commit()
            return self._normalize_user_row(row)

    def revoke_session(self, token: str):
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET is_revoked = 1 WHERE token_hash = ?",
                (token_hash,),
            )
            conn.commit()

    def login(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self._find_user_by_username(username)
        if not user or not user.get("is_active"):
            return None
        if not self._verify_password(password, user.get("password_hash", "")):
            return None
        session = self.create_session(user["id"], self.session_duration_hours)
        return {
            "token": session["token"],
            "expires_at": session["expires_at"],
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "is_active": bool(user["is_active"]),
            },
        }

    def list_permissions(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM permissions WHERE user_id = ? ORDER BY service ASC",
                (user_id,),
            ).fetchall()
            return [self._normalize_permission_row(row) for row in rows]

    def set_permissions(self, user_id: int, permissions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for item in permissions or []:
            service = self._normalize_resource(str(item.get("service", "")).strip())
            if not service:
                continue
            normalized.append(
                {
                    "service": service,
                    "can_read": 1 if bool(item.get("can_read")) else 0,
                    "can_write": 1 if bool(item.get("can_write")) else 0,
                    "can_execute": 1 if bool(item.get("can_execute")) else 0,
                }
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM permissions WHERE user_id = ?", (user_id,))
            for item in normalized:
                conn.execute(
                    """
                    INSERT INTO permissions (user_id, service, can_read, can_write, can_execute)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        item["service"],
                        item["can_read"],
                        item["can_write"],
                        item["can_execute"],
                    ),
                )
            conn.commit()
        return self.list_permissions(user_id)

    def create_user(
        self,
        admin_user_id: Optional[int],
        username: str,
        password: str,
        role: str = "user",
        permissions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        username = (username or "").strip()
        role = (role or "user").strip().lower()
        if not username:
            raise ValueError("Username is required.")
        if role not in {"admin", "user"}:
            raise ValueError("Role must be 'admin' or 'user'.")
        if len(password or "") < 4:
            raise ValueError("Password is too short.")
        now = self._now()
        password_hash = self._hash_password(password)
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, is_active, created_at, created_by, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (username, password_hash, role, now, admin_user_id, now),
                )
                user_id = int(cursor.lastrowid)
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError("Username already exists.")

        if role == "admin":
            self.set_permissions(
                user_id,
                [{"service": "all", "can_read": True, "can_write": True, "can_execute": True}],
            )
        elif permissions:
            self.set_permissions(user_id, permissions)
        else:
            self.set_permissions(
                user_id,
                [{"service": "all", "can_read": True, "can_write": False, "can_execute": False}],
            )

        user = self._find_user_by_id(user_id)
        return {
            "id": user_id,
            "username": user["username"],
            "role": user["role"],
            "is_active": user["is_active"],
            "created_at": user["created_at"],
            "permissions": self.list_permissions(user_id),
        }

    def set_password(self, user_id: int, new_password: str):
        if len(new_password or "") < 4:
            raise ValueError("Password is too short.")
        if not self._find_user_by_id(user_id):
            raise ValueError("User not found.")
        hashed = self._hash_password(new_password)
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hashed, now, user_id),
            )
            conn.commit()

    def list_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            user = self._normalize_user_row(row)
            user["permissions"] = self.list_permissions(user["id"])
            result.append(user)
        return result

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self._find_user_by_id(int(user_id))
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
            "permissions": self.list_permissions(row["id"]),
        }

    def set_user_active(self, user_id: int, is_active: bool):
        if not self._find_user_by_id(int(user_id)):
            raise ValueError("User not found.")
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if bool(is_active) else 0, now, int(user_id)),
            )
            conn.commit()

    def has_permissions(self, user: Dict[str, Any], resource_type: str, capabilities: List[str]) -> bool:
        if not user or not user.get("id"):
            return False
        if user.get("role") == "admin":
            return True

        resource = self._normalize_resource(resource_type or "")
        required = {str(cap).strip().lower() for cap in capabilities if str(cap).strip()}
        if not required:
            return True

        perms = self.list_permissions(int(user["id"]))
        candidates = [p for p in perms if p["service"] in {resource, "all", "*"}]
        if not candidates:
            return False

        for capability in required:
            field = {
                "read": "can_read",
                "write": "can_write",
                "execute": "can_execute",
            }.get(capability)
            if not field:
                continue
            if not any(bool(item.get(field)) for item in candidates):
                return False
        return True

    def log_deployment(
        self,
        request_id: str,
        user: Dict[str, Any],
        action: Optional[str],
        resource_type: Optional[str],
        resource_name: Optional[str],
        region: Optional[str],
        environment: Optional[str],
        status: str,
        request_text: Optional[str],
        execution_summary: Optional[Dict[str, Any]],
    ):
        now = self._now()
        summary_json = None
        if execution_summary is not None:
            try:
                summary_json = json.dumps(execution_summary, ensure_ascii=True)
            except Exception:
                summary_json = json.dumps({"note": "summary serialization failed"})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments (
                    request_id, user_id, username, action, resource_type, resource_name, region,
                    environment, status, request_text, execution_summary, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    int(user["id"]),
                    str(user.get("username") or ""),
                    action,
                    self._normalize_resource(resource_type or ""),
                    resource_name,
                    region,
                    environment,
                    status,
                    request_text,
                    summary_json,
                    now,
                ),
            )
            conn.commit()

    def list_deployments(self, user: Dict[str, Any], limit: int = 100, all_users: bool = False) -> List[Dict[str, Any]]:
        user_id = int(user["id"])
        limit = max(1, min(int(limit or 100), 500))
        with self._connect() as conn:
            if all_users and user.get("role") == "admin":
                rows = conn.execute(
                    "SELECT * FROM deployments ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deployments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()

        result = []
        for row in rows:
            summary = None
            if row["execution_summary"]:
                try:
                    summary = json.loads(row["execution_summary"])
                except Exception:
                    summary = {"raw": row["execution_summary"]}
            result.append(
                {
                    "id": row["id"],
                    "request_id": row["request_id"],
                    "username": row["username"],
                    "action": row["action"],
                    "resource_type": row["resource_type"],
                    "resource_name": row["resource_name"],
                    "region": row["region"],
                    "environment": row["environment"],
                    "status": row["status"],
                    "request_text": row["request_text"],
                    "execution_summary": summary,
                    "created_at": self._ts_to_iso(row["created_at"]),
                }
            )
        return result

    def list_deployments_by_request(
        self,
        user: Dict[str, Any],
        request_id: str,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not request_id:
            return []
        user_id = int(user["id"])
        limit = max(1, min(int(limit or 200), 500))
        with self._connect() as conn:
            if user.get("role") == "admin":
                rows = conn.execute(
                    """
                    SELECT * FROM deployments
                    WHERE request_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (str(request_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM deployments
                    WHERE request_id = ? AND user_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (str(request_id), user_id, limit),
                ).fetchall()

        result = []
        for row in rows:
            summary = None
            if row["execution_summary"]:
                try:
                    summary = json.loads(row["execution_summary"])
                except Exception:
                    summary = {"raw": row["execution_summary"]}
            result.append(
                {
                    "id": row["id"],
                    "request_id": row["request_id"],
                    "username": row["username"],
                    "action": row["action"],
                    "resource_type": row["resource_type"],
                    "resource_name": row["resource_name"],
                    "region": row["region"],
                    "environment": row["environment"],
                    "status": row["status"],
                    "request_text": row["request_text"],
                    "execution_summary": summary,
                    "created_at": self._ts_to_iso(row["created_at"]),
                }
            )
        return result

    def create_remediation_run(
        self,
        run_id: str,
        request_id: str,
        user: Dict[str, Any],
        plan: Dict[str, Any],
        request_snapshot: Optional[Dict[str, Any]] = None,
        resume_context: Optional[Dict[str, Any]] = None,
        required_permissions: Optional[List[str]] = None,
        approval_scope: str = "request_run",
        expires_in_seconds: int = 3600,
    ) -> Dict[str, Any]:
        now = self._now()
        exp = now + max(60, int(expires_in_seconds or 3600))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO remediation_runs (
                    run_id, request_id, user_id, username, status, plan_json, request_snapshot_json,
                    resume_context_json, required_permissions_json, result_json, attempts,
                    approval_scope, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(request_id),
                    int(user["id"]),
                    str(user.get("username") or ""),
                    "pending_approval",
                    self._json_dump(plan) or "{}",
                    self._json_dump(request_snapshot),
                    self._json_dump(resume_context),
                    self._json_dump(required_permissions or []),
                    None,
                    0,
                    str(approval_scope or "request_run"),
                    exp,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_remediation_run(run_id=str(run_id), request_id=str(request_id), user=user) or {}

    def get_remediation_run(self, run_id: str, request_id: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not run_id or not request_id:
            return None
        with self._connect() as conn:
            if user.get("role") == "admin":
                row = conn.execute(
                    """
                    SELECT * FROM remediation_runs
                    WHERE run_id = ? AND request_id = ?
                    LIMIT 1
                    """,
                    (str(run_id), str(request_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM remediation_runs
                    WHERE run_id = ? AND request_id = ? AND user_id = ?
                    LIMIT 1
                    """,
                    (str(run_id), str(request_id), int(user["id"])),
                ).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "request_id": row["request_id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "status": row["status"],
            "plan": self._json_load(row["plan_json"]) or {},
            "request_snapshot": self._json_load(row["request_snapshot_json"]) or {},
            "resume_context": self._json_load(row["resume_context_json"]) or {},
            "required_permissions": self._json_load(row["required_permissions_json"]) or [],
            "result": self._json_load(row["result_json"]),
            "attempts": int(row["attempts"] or 0),
            "approval_scope": row["approval_scope"] or "request_run",
            "expires_at": self._ts_to_iso(row["expires_at"]) if row["expires_at"] else None,
            "is_expired": bool(row["expires_at"] and int(row["expires_at"]) <= self._now()),
            "created_at": self._ts_to_iso(row["created_at"]),
            "updated_at": self._ts_to_iso(row["updated_at"]),
        }

    def update_remediation_run(
        self,
        run_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        increment_attempt: bool = False,
    ):
        if not run_id:
            return
        now = self._now()
        with self._connect() as conn:
            if increment_attempt:
                conn.execute(
                    """
                    UPDATE remediation_runs
                    SET status = ?, result_json = ?, attempts = attempts + 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (str(status), self._json_dump(result), now, str(run_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE remediation_runs
                    SET status = ?, result_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (str(status), self._json_dump(result), now, str(run_id)),
                )
            conn.commit()

    def log_approval_event(
        self,
        run_id: str,
        request_id: str,
        user: Dict[str, Any],
        approved: bool,
        scope: str = "request_run",
        note: Optional[str] = None,
    ):
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_events (run_id, request_id, user_id, username, approved, scope, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(request_id),
                    int(user["id"]),
                    str(user.get("username") or ""),
                    1 if bool(approved) else 0,
                    str(scope or "request_run"),
                    str(note or ""),
                    now,
                ),
            )
            conn.commit()

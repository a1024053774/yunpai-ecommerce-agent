from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .database import Database, utc_now


class AuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    client_id: str
    subject_hash: str
    can_supply_order_context: bool


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    tenant_id: str
    admin_id: str
    capabilities: frozenset[str] = frozenset()


# M10-R 写职责能力（D-035：唯一权威定义，路由层只引用常量，不复制字符串）。
CAPABILITY_FINANCE_POLICY_WRITE = "finance:policy:write"
CAPABILITY_FINANCE_LEDGER_WRITE = "finance:ledger:write"
CAPABILITY_ORDERING_DRAFT_WRITE = "ordering:draft:write"
CAPABILITY_ORDERING_CONFIRM_WRITE = "ordering:confirm:write"
CAPABILITY_ORDERING_ADVANCE_WRITE = "ordering:advance:write"

M10_WRITE_CAPABILITIES = frozenset(
    {
        CAPABILITY_FINANCE_POLICY_WRITE,
        CAPABILITY_FINANCE_LEDGER_WRITE,
        CAPABILITY_ORDERING_DRAFT_WRITE,
        CAPABILITY_ORDERING_CONFIRM_WRITE,
        CAPABILITY_ORDERING_ADVANCE_WRITE,
    }
)

# 每个 capability 由独立环境变量按 admin_id 白名单授权（服务端默认拒绝）。
_CAPABILITY_ENV: dict[str, str] = {
    "finance:final_profit:read": "FINAL_PROFIT_READ_ADMIN_IDS",
    CAPABILITY_FINANCE_POLICY_WRITE: "M10_FINANCE_POLICY_WRITE_ADMIN_IDS",
    CAPABILITY_FINANCE_LEDGER_WRITE: "M10_FINANCE_LEDGER_WRITE_ADMIN_IDS",
    CAPABILITY_ORDERING_DRAFT_WRITE: "M10_ORDERING_DRAFT_WRITE_ADMIN_IDS",
    CAPABILITY_ORDERING_CONFIRM_WRITE: "M10_ORDERING_CONFIRM_WRITE_ADMIN_IDS",
    CAPABILITY_ORDERING_ADVANCE_WRITE: "M10_ORDERING_ADVANCE_WRITE_ADMIN_IDS",
}


def _admin_capabilities(admin_id: str) -> frozenset[str]:
    granted: set[str] = set()
    for capability, env_name in _CAPABILITY_ENV.items():
        allowed = {
            item.strip()
            for item in os.environ.get(env_name, "").split(",")
            if item.strip()
        }
        if admin_id in allowed:
            granted.add(capability)
    return frozenset(granted)


class AdminOperatorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin_id: str = Field(
        min_length=3, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$"
    )
    name: str = Field(min_length=2, max_length=160)
    key: str = Field(min_length=24, max_length=256)


class AdminOperatorStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_status: str = Field(pattern=r"^active$")
    reason: str = Field(min_length=3, max_length=300)


class AuthenticationService:
    KEY_ITERATIONS = 210_000

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        if settings.auth_required:
            if settings.bootstrap_client_key:
                self._ensure_client(
                    client_id=settings.bootstrap_client_id,
                    tenant_id=settings.bootstrap_tenant_id,
                    name="Bootstrap upstream adapter",
                    key=settings.bootstrap_client_key,
                    can_supply_order_context=settings.bootstrap_client_can_supply_order_context,
                    role="client",
                )
            if settings.admin_api_key:
                self._ensure_client(
                    client_id=settings.bootstrap_admin_id,
                    tenant_id=settings.bootstrap_tenant_id,
                    name="Bootstrap appliance administrator",
                    key=settings.admin_api_key,
                    can_supply_order_context=False,
                    role="admin",
                )
        else:
            self._ensure_client(
                client_id="anonymous-local",
                tenant_id="anonymous-local",
                name="Authentication-disabled local client",
                key="internal-auth-disabled-client",
                can_supply_order_context=False,
                role="client",
            )

    @property
    def configured(self) -> bool:
        if not self.settings.auth_required:
            return True
        if not self.settings.subject_hash_key:
            return False
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_clients WHERE status='active' AND role='client'"
            ).fetchone()
        return bool(row[0])

    @property
    def admin_configured(self) -> bool:
        if not self.settings.admin_auth_required:
            return True
        if not self.settings.admin_api_key:
            return False
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_clients WHERE status='active' AND role='admin'"
            ).fetchone()
        return bool(row[0])

    def authenticate(
        self,
        client_id: str | None,
        client_key: str | None,
        subject_id: str | None,
    ) -> Principal:
        if not self.settings.auth_required:
            return Principal(
                tenant_id="anonymous-local",
                client_id="anonymous-local",
                subject_hash=self._subject_hash("anonymous-local", subject_id or "local-user"),
                can_supply_order_context=False,
            )
        if not self.configured:
            raise AuthError("client authentication is required but no active client is configured")
        if not client_id or not client_key or not subject_id:
            raise AuthError("X-Client-Id, X-Client-Key and X-Subject-Id are required")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_clients WHERE id=? AND status='active' AND role='client'",
                (client_id,),
            ).fetchone()
        if row is None or not self._verify_key(
            client_key, row["key_salt"], row["key_hash"], row["key_iterations"]
        ):
            raise AuthError("invalid client credentials")
        return Principal(
            tenant_id=str(row["tenant_id"]),
            client_id=str(row["id"]),
            subject_hash=self._subject_hash(str(row["tenant_id"]), subject_id),
            can_supply_order_context=bool(row["can_supply_order_context"]),
        )

    def authenticate_admin(self, admin_id: str | None, admin_key: str | None) -> AdminPrincipal:
        if not self.settings.admin_auth_required:
            # 本地/未启用管理员认证时视为可信开发环境：授予全部 M10 写职责。
            return AdminPrincipal(
                tenant_id=self.settings.bootstrap_tenant_id,
                admin_id=self.settings.bootstrap_admin_id,
                capabilities=_admin_capabilities(
                    self.settings.bootstrap_admin_id
                ) | M10_WRITE_CAPABILITIES,
            )
        if not self.admin_configured:
            raise AuthError("administrator authentication is not configured")
        if not admin_id or not admin_key:
            raise AuthError("X-Admin-Id and X-Admin-Key are required")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_clients WHERE id=? AND status='active' AND role='admin'",
                (admin_id,),
            ).fetchone()
        if row is None or not self._verify_key(
            admin_key, row["key_salt"], row["key_hash"], row["key_iterations"]
        ):
            raise AuthError("invalid administrator credentials")
        return AdminPrincipal(
            tenant_id=str(row["tenant_id"]),
            admin_id=str(row["id"]),
            capabilities=_admin_capabilities(str(row["id"])),
        )

    def create_admin_operator(
        self,
        tenant_id: str,
        request: AdminOperatorCreateRequest,
        actor: str,
    ) -> dict:
        salt = os.urandom(16)
        key_hash = self._derive_key(request.key, salt, self.KEY_ITERATIONS)
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM api_clients WHERE id=?", (request.admin_id,)
            ).fetchone()
            if existing is not None:
                raise AuthError("administrator id already exists")
            conn.execute(
                """
                INSERT INTO api_clients(
                    id, tenant_id, name, key_salt, key_hash, key_iterations,
                    can_supply_order_context, status, created_at, updated_at, role
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, 'admin')
                """,
                (
                    request.admin_id,
                    tenant_id,
                    request.name,
                    salt,
                    key_hash,
                    self.KEY_ITERATIONS,
                    now,
                    now,
                ),
            )
        self.db.audit(
            "administrator.created",
            actor,
            request.admin_id,
            {"name": request.name},
            tenant_id,
        )
        return {
            "admin_id": request.admin_id,
            "name": request.name,
            "status": "active",
            "created_at": now,
        }

    def list_admin_operators(self, tenant_id: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id AS admin_id, name, status, created_at, updated_at
                FROM api_clients
                WHERE tenant_id=? AND role='admin'
                ORDER BY status, created_at, id
                """,
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def disable_admin_operator(
        self,
        tenant_id: str,
        admin_id: str,
        request: AdminOperatorStatusRequest,
        actor: str,
    ) -> dict:
        if admin_id == actor:
            raise AuthError("administrator cannot disable the current credential")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            assigned = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM handoff_tasks
                    WHERE tenant_id=? AND assigned_to=?
                      AND status IN ('accepted','working','input_required','review')
                    """,
                    (tenant_id, admin_id),
                ).fetchone()[0]
            )
            if assigned:
                raise AuthError(
                    "administrator has active handoff tasks and must be reassigned first"
                )
            cursor = conn.execute(
                """
                UPDATE api_clients SET status='disabled', updated_at=?
                WHERE id=? AND tenant_id=? AND role='admin' AND status=?
                """,
                (now, admin_id, tenant_id, request.expected_status),
            )
            if cursor.rowcount != 1:
                raise AuthError("administrator status transition failed")
            conn.execute(
                """
                UPDATE handoff_operator_profiles
                SET status='inactive', presence='offline', presence_updated_at=?,
                    presence_expires_at=NULL, record_version=record_version+1,
                    updated_at=?
                WHERE tenant_id=? AND admin_id=?
                """,
                (now, now, tenant_id, admin_id),
            )
            saved = conn.execute(
                """
                SELECT id AS admin_id, name, status, created_at, updated_at
                FROM api_clients WHERE id=?
                """,
                (admin_id,),
            ).fetchone()
        self.db.audit(
            "administrator.disabled",
            actor,
            admin_id,
            {"reason": request.reason},
            tenant_id,
        )
        return dict(saved)

    def _ensure_client(
        self,
        *,
        client_id: str,
        tenant_id: str,
        name: str,
        key: str,
        can_supply_order_context: bool,
        role: str,
    ) -> None:
        if len(key) < 16:
            raise AuthError("client key must contain at least 16 characters")
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM api_clients WHERE id=?", (client_id,)).fetchone()
            if existing is not None and self._verify_key(
                key, existing["key_salt"], existing["key_hash"], existing["key_iterations"]
            ):
                if (
                    existing["tenant_id"] == tenant_id
                    and bool(existing["can_supply_order_context"]) == can_supply_order_context
                    and existing["role"] == role
                    and existing["status"] == "active"
                ):
                    return
            salt = os.urandom(16)
            key_hash = self._derive_key(key, salt, self.KEY_ITERATIONS)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO api_clients(
                    id, tenant_id, name, key_salt, key_hash, key_iterations,
                    can_supply_order_context, status, created_at, updated_at, role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    name=excluded.name,
                    key_salt=excluded.key_salt,
                    key_hash=excluded.key_hash,
                    key_iterations=excluded.key_iterations,
                    can_supply_order_context=excluded.can_supply_order_context,
                    role=excluded.role,
                    status='active',
                    updated_at=excluded.updated_at
                """,
                (
                    client_id, tenant_id, name, salt, key_hash, self.KEY_ITERATIONS,
                    int(can_supply_order_context), now, now, role,
                ),
            )

    @staticmethod
    def _derive_key(key: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, iterations)

    @classmethod
    def _verify_key(cls, key: str, salt: bytes, expected: bytes, iterations: int) -> bool:
        actual = cls._derive_key(key, salt, int(iterations))
        return hmac.compare_digest(actual, expected)

    def _subject_hash(self, tenant_id: str, subject_id: str) -> str:
        secret = self.settings.subject_hash_key or "auth-disabled-local-hash-key"
        return hmac.new(
            secret.encode("utf-8"),
            f"{tenant_id}\x1f{subject_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

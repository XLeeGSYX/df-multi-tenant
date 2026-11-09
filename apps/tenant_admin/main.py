"""tenant-admin entrypoint (design doc §1.2 service 1 — management plane).

NEVER imports deerflow. Owns:

- tenant lifecycle: provision/deprovision directories + DB rows (tenant-db)
- the per-tenant user registry (tenant-db ``tenant_users``) and JWT issuance
  carrying the ``tenant`` claim plus an optional ``user`` claim naming a
  registered user (RS256, keypair persisted on first boot), JWKS distribution
  for agent-runtime verification
- an append-only audit log for every management mutation

Permission isolation: user rows carry a ``role`` ("admin" | "user") that rides
the token as a ``role`` claim. The agent-configuration endpoints below accept
 EITHER the operator key OR a Bearer token whose tenant matches the path and
whose role is admin-capable (tenant-level tokens and admin-role users; see
``tenancy.context.is_admin_role``); user-role tokens get 403. Tenant/user
lifecycle stays operator-key territory.

Demo hardening: bind to loopback (see Makefile) and require the
``X-Admin-Key`` header when ``TENANT_ADMIN_API_KEY`` is set. Real
deployments front this with operator SSO.

Run from the df-multitenant repo root:

    uv run --project apps/tenant_admin \
        uvicorn apps.tenant_admin.main:app --port 8010
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from tenancy.context import is_admin_role, is_valid_tenant_id, is_valid_user_name
from tenancy.security import ROLE_CLAIM, TENANT_CLAIM, USER_CLAIM, issue_jwt
from tenant_db import agent_config
from tenant_db.models import AuditLog, Base, LeadAgent, Skill, SubAgent, Tenant, TenantUser
from tenant_db.provision import deprovision_tenant, provision_tenant
from tenant_db.schema_upgrade import ensure_schema
from tenant_db.tenancy_events import install_tenancy_events

REPO_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_ROOT = Path(os.environ.get("DF_FOUNDATION_ROOT", REPO_ROOT / ".foundation"))
TRASH_DIR = Path(os.environ.get("DF_TENANT_TRASH_DIR", FOUNDATION_ROOT / "_trash"))
SECRETS_DIR = Path(os.environ.get("TENANT_ADMIN_SECRETS_DIR", REPO_ROOT / ".secrets"))
DB_PATH = Path(os.environ.get("TENANT_ADMIN_DB", REPO_ROOT / ".runtime" / "tenant_admin.db"))


# ── Signing keys ──────────────────────────────────────────────────────────


def _b64url_int(value: int) -> str:
    import base64

    byte_len = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(byte_len, "big")).rstrip(b"=").decode()


def _rsa_public_jwk_fields(public_key) -> dict:
    """RFC 7518 RSA public JWK fields (n, e)."""
    numbers = public_key.public_numbers()
    return {"n": _b64url_int(numbers.n), "e": _b64url_int(numbers.e)}


def _load_or_create_signing_key() -> tuple[str, str, dict]:
    """Return ``(kid, private PEM text, public JWK dict)`` for the persisted keypair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    private_pem = SECRETS_DIR / "tenant_jwt_rs256.key"
    public_pem = SECRETS_DIR / "tenant_jwt_rs256.pub"
    if not private_pem.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_pem.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        private_pem.chmod(0o600)

    private_key = serialization.load_pem_private_key(private_pem.read_bytes(), password=None)
    public_key = serialization.load_pem_public_key(public_pem.read_bytes())
    kid = uuid.uuid5(uuid.NAMESPACE_URL, f"df-multitenant:{public_pem.read_text()}").hex[:16]
    public_jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        **_rsa_public_jwk_fields(public_key),
    }
    return kid, private_pem.read_text(), public_jwk


_KID, _PRIVATE_PEM, _PUBLIC_JWK = _load_or_create_signing_key()
# Local verification of tokens this service issued (the persisted public key
# halves the JWKS round-trip for admin-role checks on the config endpoints).
_PUBLIC_PEM = (SECRETS_DIR / "tenant_jwt_rs256.pub").read_text()


# ── Persistence ────────────────────────────────────────────────────────────


def _make_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    # create_all never alters existing tables — bring pre-role databases up
    # to the current column set (no-op on fresh ones).
    ensure_schema(engine)
    # Listening on the Session base class keeps admin sessions covered no
    # matter which factory produces them (design doc §6.2).
    install_tenancy_events()
    return sessionmaker(bind=engine, expire_on_commit=False)


_SESSION_FACTORY = _make_session_factory()


# ── App ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="DeerFlow tenant-admin", version="0.1.0", lifespan=lifespan)


def _check_admin_key(request: Request) -> None:
    expected = os.environ.get("TENANT_ADMIN_API_KEY")
    if expected and request.headers.get("x-admin-key") != expected:
        raise HTTPException(status_code=401, detail="invalid admin key")


def _operator_key_valid(request: Request) -> bool:
    """True when the operator key is configured AND the request carries it."""
    expected = os.environ.get("TENANT_ADMIN_API_KEY")
    return bool(expected) and request.headers.get("x-admin-key") == expected


def _verify_bearer(request: Request) -> dict | None:
    """Verify a Bearer token against this service's own persisted public key.

    Same claim contract as the data plane's ``tenancy.security.verify_jwt``
    (exp/sub/tenant required); ``None`` on any failure — callers translate.
    """
    import jwt as pyjwt

    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    try:
        return pyjwt.decode(
            token,
            _PUBLIC_PEM,
            algorithms=["RS256"],
            options={"require": ["exp", "sub", TENANT_CLAIM]},
            leeway=30,
        )
    except Exception:
        return None


def _require_agent_config_admin(request: Request, tenant_id: str) -> None:
    """Permission gate for the agent-configuration endpoints.

    Accepted credentials, in order:

    1. the operator key (``X-Admin-Key``) — platform operators outrank the
       tenant's own role model;
    2. a Bearer JWT issued by this service whose ``tenant`` claim matches the
       path and whose role is admin-capable — tenant-level tokens (no ``role``
       claim, the tenant's master identity) and explicit ``admin``-role users.

    A valid but non-admin user token is 403 (not 401): the caller IS
    authenticated, just not allowed to configure agents.
    """
    if _operator_key_valid(request):
        return
    claims = _verify_bearer(request)
    if claims is None:
        raise HTTPException(status_code=401, detail="agent configuration requires the operator key or a tenant token")
    if claims.get(TENANT_CLAIM) != tenant_id:
        raise HTTPException(status_code=403, detail="token tenant does not match this tenant")
    role = claims.get(ROLE_CLAIM)
    if not is_admin_role(role):
        raise HTTPException(status_code=403, detail="admin role required to configure agents")


class TenantCreate(BaseModel):
    id: str = Field(min_length=2, max_length=63, pattern=r"^[a-z][a-z0-9-]{1,62}$")
    name: str


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    display_name: str = ""
    role: str = Field(default="user", pattern=r"^(admin|user)$")


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = Field(default=None, pattern=r"^(admin|user)$")


class TokenRequest(BaseModel):
    subject: str = "tenant-admin"
    # Optional user WITHIN the tenant: mints a user-scoped token whose data
    # plane identity (owner axis) becomes tenant-<tid>-<user>.
    user: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    expires_in_seconds: int = Field(default=3600, gt=0, le=86400)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict:
    return {"status": "healthy", "service": "tenant-admin"}


@app.get("/.well-known/jwks.json", tags=["auth"])
async def jwks() -> dict:
    return {"keys": [dict(_PUBLIC_JWK)]}


# DB-backed handlers are plain ``def`` so FastAPI runs them in its thread
# pool instead of blocking the event loop on sqlite calls.
@app.post("/admin/tenants", status_code=201, tags=["tenants"])
def create_tenant(body: TenantCreate, request: Request) -> dict:
    _check_admin_key(request)
    if not is_valid_tenant_id(body.id):
        raise HTTPException(status_code=400, detail=f"invalid or reserved tenant id: {body.id}")
    with _SESSION_FACTORY() as session:
        if session.get(Tenant, body.id) is not None:
            raise HTTPException(status_code=409, detail="tenant already exists")
        try:
            root = provision_tenant(body.id, FOUNDATION_ROOT)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.add(Tenant(id=body.id, name=body.name))
        session.add(AuditLog(tenant_id=body.id, actor="tenant-admin", action="tenant.create", detail=json.dumps({"root": str(root)})))
        session.commit()
    return {"id": body.id, "name": body.name, "status": "active"}


@app.get("/admin/tenants", tags=["tenants"])
def list_tenants(request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        rows = session.execute(select(Tenant)).scalars().all()
        return {"tenants": [{"id": r.id, "name": r.name, "status": r.status} for r in rows]}


@app.post("/admin/tenants/{tenant_id}/disable", tags=["tenants"])
def disable_tenant(tenant_id: str, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        row = session.get(Tenant, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        if row.status == "active":
            try:
                trash = deprovision_tenant(tenant_id, FOUNDATION_ROOT, TRASH_DIR)
            except FileNotFoundError:
                trash = None  # directory already gone; DB state still moves on
            row.status = "disabled"
            session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="tenant.disable", detail=json.dumps({"moved_to": str(trash)})))
            session.commit()
    return {"id": tenant_id, "status": "disabled"}


@app.post("/admin/tenants/{tenant_id}/enable", tags=["tenants"])
def enable_tenant(tenant_id: str, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        row = session.get(Tenant, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        if row.status != "active":
            try:
                provision_tenant(tenant_id, FOUNDATION_ROOT)
            except FileExistsError:
                pass  # directory survived an earlier failed disable
            row.status = "active"
            session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="tenant.enable", detail=""))
            session.commit()
    return {"id": tenant_id, "status": "active"}


@app.delete("/admin/tenants/{tenant_id}", tags=["tenants"])
def delete_tenant(tenant_id: str, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        row = session.get(Tenant, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        if row.status == "active":
            try:
                deprovision_tenant(tenant_id, FOUNDATION_ROOT, TRASH_DIR)
            except FileNotFoundError:
                pass
        # Users and agent-config rows belong to the tenant; drop them with it.
        # (The materialized files live under the tenant root, which the
        # deprovision above already trashed.)
        for model in (TenantUser, LeadAgent, Skill, SubAgent):
            for orphan in session.execute(
                select(model).where(model.tenant_id == tenant_id)
            ).scalars():
                session.delete(orphan)
        session.delete(row)
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="tenant.delete", detail=""))
        session.commit()
    return {"id": tenant_id, "deleted": True}


# ── Users within a tenant ──────────────────────────────────────────────────


def _require_active_tenant(session, tenant_id: str) -> Tenant:
    row = session.get(Tenant, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return row


@app.get("/admin/tenants/{tenant_id}/users", tags=["users"])
def list_users(tenant_id: str, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        rows = (
            session.execute(
                select(TenantUser).where(TenantUser.tenant_id == tenant_id).order_by(TenantUser.username)
            )
            .scalars()
            .all()
        )
        return {
            "users": [
                {
                    "username": r.username,
                    "display_name": r.display_name,
                    "role": r.role,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }


@app.post("/admin/tenants/{tenant_id}/users", status_code=201, tags=["users"])
def create_user(tenant_id: str, body: UserCreate, request: Request) -> dict:
    _check_admin_key(request)
    if not is_valid_user_name(body.username):
        raise HTTPException(status_code=400, detail=f"invalid username: {body.username}")
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        existing = session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.username == body.username)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="user already exists")
        session.add(TenantUser(tenant_id=tenant_id, username=body.username, display_name=body.display_name, role=body.role))
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor="tenant-admin",
                action="user.create",
                detail=json.dumps({"username": body.username, "role": body.role}),
            )
        )
        session.commit()
    return {"username": body.username, "display_name": body.display_name, "role": body.role}


@app.put("/admin/tenants/{tenant_id}/users/{username}", tags=["users"])
def update_user(tenant_id: str, username: str, body: UserUpdate, request: Request) -> dict:
    """Update a registry user — role changes are the admin-axis lever."""
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        row = session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.username == username)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        changes: dict[str, str] = {}
        if body.display_name is not None and body.display_name != row.display_name:
            row.display_name = body.display_name
            changes["display_name"] = body.display_name
        if body.role is not None and body.role != row.role:
            row.role = body.role
            changes["role"] = body.role
        if changes:
            session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="user.update", detail=json.dumps({"username": username, **changes})))
            session.commit()
        return {"username": username, "display_name": row.display_name, "role": row.role}


@app.delete("/admin/tenants/{tenant_id}/users/{username}", tags=["users"])
def delete_user(tenant_id: str, username: str, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        row = session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.username == username)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        session.delete(row)
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="user.delete", detail=json.dumps({"username": username})))
        session.commit()
    return {"username": username, "deleted": True}


# ── Per-tenant agent configuration (lead agent / subagents / skills) ───────
#
# DB rows are the source of truth; every mutation also materializes the
# deerflow-native files under the tenant's foundation root (see
# tenant_db.agent_config) inside the DB transaction, so a file-write failure
# rolls the row back. The runtime re-reads these per agent build (soul,
# subagents via a ~1s signature cache) or through the tenant-scoped skills
# patch's invalidation, so changes apply to the next message.


def _require_configurable_tenant(session, tenant_id: str) -> Tenant:
    """Tenant must exist AND be active: a disabled tenant's root is trashed."""
    row = _require_active_tenant(session, tenant_id)
    if row.status != "active":
        raise HTTPException(status_code=409, detail="tenant is disabled")
    return row


def _tenant_root(tenant_id: str) -> Path:
    return FOUNDATION_ROOT / tenant_id


class LeadAgentUpdate(BaseModel):
    soul_md: str | None = Field(default=None, max_length=65536)


class SubagentCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    display_name: str = ""
    description: str = Field(min_length=1, max_length=4096)
    system_prompt: str = Field(min_length=1, max_length=65536)
    tools: list[str] | None = None
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = Field(default=50, ge=1, le=200)
    timeout_seconds: int = Field(default=900, ge=1, le=86400)
    enabled: bool = True


class SubagentUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=4096)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=65536)
    tools: list[str] | None = None
    skills: list[str] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=255)
    max_turns: int | None = Field(default=None, ge=1, le=200)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    enabled: bool | None = None


class SkillCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(min_length=1, max_length=4096)
    content: str = Field(default="", max_length=262144)


class SkillUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=4096)
    content: str | None = Field(default=None, max_length=262144)
    enabled: bool | None = None


@app.get("/admin/tenants/{tenant_id}/lead-agent", tags=["agent-config"])
def get_lead_agent(tenant_id: str, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        row = session.get(LeadAgent, tenant_id)
        return {
            "soul_md": row.soul_md if row is not None else None,
            "updated_at": row.updated_at.isoformat() if row is not None else None,
        }


@app.put("/admin/tenants/{tenant_id}/lead-agent", tags=["agent-config"])
def update_lead_agent(tenant_id: str, body: LeadAgentUpdate, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    soul = body.soul_md.strip() if body.soul_md is not None else None
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        row = session.get(LeadAgent, tenant_id)
        if soul:
            if row is None:
                session.add(LeadAgent(tenant_id=tenant_id, soul_md=soul))
            else:
                row.soul_md = soul
        elif row is not None:
            session.delete(row)
        # Materialize inside the transaction: a write failure rolls back the row.
        agent_config.write_soul(_tenant_root(tenant_id), soul)
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor="tenant-admin",
                action="lead_agent.update",
                detail=json.dumps({"cleared": soul is None, "length": len(soul or "")}),
            )
        )
        session.commit()
    return {"soul_md": soul or None}


def _subagent_response(row: SubAgent) -> dict:
    definition = agent_config.read_subagent(_tenant_root(row.tenant_id), row.name) or {}
    return {
        "name": row.name,
        "display_name": definition.get("display_name"),
        "description": definition.get("description", ""),
        "system_prompt": definition.get("system_prompt", ""),
        "tools": definition.get("tools"),
        "skills": definition.get("skills"),
        "model": definition.get("model", "inherit"),
        "max_turns": definition.get("max_turns", 50),
        "timeout_seconds": definition.get("timeout_seconds", 900),
        "enabled": row.enabled,
    }


@app.get("/admin/tenants/{tenant_id}/subagents", tags=["agent-config"])
def list_subagents(tenant_id: str, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        rows = (
            session.execute(select(SubAgent).where(SubAgent.tenant_id == tenant_id).order_by(SubAgent.name))
            .scalars()
            .all()
        )
        return {"subagents": [_subagent_response(r) for r in rows]}


@app.post("/admin/tenants/{tenant_id}/subagents", status_code=201, tags=["agent-config"])
def create_subagent(tenant_id: str, body: SubagentCreate, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        if body.name in agent_config.BUILTIN_SUBAGENT_NAMES:
            raise HTTPException(status_code=400, detail=f"name conflicts with a built-in subagent: {body.name}")
        existing = session.execute(
            select(SubAgent).where(SubAgent.tenant_id == tenant_id, SubAgent.name == body.name)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="subagent already exists")
        if agent_config.subagent_file(_tenant_root(tenant_id), body.name).exists():
            raise HTTPException(status_code=409, detail=f"subagent file already exists: {body.name}")
        agent_config.write_subagent(
            _tenant_root(tenant_id),
            {
                "name": body.name,
                "display_name": body.display_name or None,
                "description": body.description,
                "system_prompt": body.system_prompt,
                "tools": body.tools,
                "skills": body.skills,
                "model": body.model,
                "max_turns": body.max_turns,
                "timeout_seconds": body.timeout_seconds,
                "enabled": body.enabled,
            },
        )
        session.add(
            SubAgent(
                tenant_id=tenant_id,
                name=body.name,
                rel_path=f"{agent_config.MANAGED_SUBAGENTS_DIRNAME}/{body.name}.json",
                enabled=body.enabled,
            )
        )
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="subagent.create", detail=json.dumps({"name": body.name})))
        session.commit()
    return {"name": body.name, "enabled": body.enabled}


@app.put("/admin/tenants/{tenant_id}/subagents/{name}", tags=["agent-config"])
def update_subagent(tenant_id: str, name: str, body: SubagentUpdate, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        row = session.execute(
            select(SubAgent).where(SubAgent.tenant_id == tenant_id, SubAgent.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        definition = agent_config.read_subagent(_tenant_root(tenant_id), name) or {}
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="no fields provided")
        if "display_name" in updates:
            definition["display_name"] = updates["display_name"] or None
        for key in ("description", "system_prompt", "tools", "skills", "model", "max_turns", "timeout_seconds"):
            if key in updates:
                definition[key] = updates[key]
        if updates.get("enabled") is not None:
            definition["enabled"] = updates["enabled"]
            row.enabled = updates["enabled"]
        definition.setdefault("name", name)
        agent_config.write_subagent(_tenant_root(tenant_id), definition)
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="subagent.update", detail=json.dumps({"name": name, "fields": sorted(updates)})))
        session.commit()
    return {"name": name, "enabled": definition.get("enabled", True)}


@app.delete("/admin/tenants/{tenant_id}/subagents/{name}", tags=["agent-config"])
def delete_subagent(tenant_id: str, name: str, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        row = session.execute(
            select(SubAgent).where(SubAgent.tenant_id == tenant_id, SubAgent.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        agent_config.remove_subagent(_tenant_root(tenant_id), name)
        session.delete(row)
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="subagent.delete", detail=json.dumps({"name": name})))
        session.commit()
    return {"name": name, "deleted": True}


@app.get("/admin/tenants/{tenant_id}/skills", tags=["agent-config"])
def list_skills(tenant_id: str, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_active_tenant(session, tenant_id)
        rows = (
            session.execute(select(Skill).where(Skill.tenant_id == tenant_id).order_by(Skill.name))
            .scalars()
            .all()
        )
        skills = []
        for row in rows:
            parsed = agent_config.read_skill(_tenant_root(tenant_id), row.name)
            if parsed is None:
                continue  # registry/file drift: hide rows whose package is gone
            skills.append(
                {
                    "name": row.name,
                    "description": parsed["description"],
                    "content": parsed["content"],
                    "enabled": row.enabled,
                }
            )
        return {"skills": skills}


@app.post("/admin/tenants/{tenant_id}/skills", status_code=201, tags=["agent-config"])
def create_skill(tenant_id: str, body: SkillCreate, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        existing = session.execute(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == body.name)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="skill already exists")
        if agent_config.skill_file(_tenant_root(tenant_id), body.name).exists():
            raise HTTPException(status_code=409, detail=f"skill file already exists: {body.name}")
        agent_config.write_skill(_tenant_root(tenant_id), body.name, body.description, body.content)
        session.add(
            Skill(
                tenant_id=tenant_id,
                name=body.name,
                rel_path=f"{agent_config.SKILLS_DIRNAME}/{agent_config.SKILL_CUSTOM_DIRNAME}/{body.name}",
                version=1,
                enabled=True,
            )
        )
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="skill.create", detail=json.dumps({"name": body.name})))
        session.commit()
    return {"name": body.name, "enabled": True}


@app.put("/admin/tenants/{tenant_id}/skills/{name}", tags=["agent-config"])
def update_skill(tenant_id: str, name: str, body: SkillUpdate, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        row = session.execute(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="skill not found")
        parsed = agent_config.read_skill(_tenant_root(tenant_id), name)
        if parsed is None:
            raise HTTPException(status_code=409, detail=f"skill package missing on disk: {name}")
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="no fields provided")
        if "description" in updates:
            parsed["description"] = updates["description"]
        if "content" in updates:
            parsed["content"] = updates["content"]
        if "description" in updates or "content" in updates:
            agent_config.write_skill(_tenant_root(tenant_id), name, parsed["description"], parsed["content"])
            row.version = (row.version or 1) + 1
        if updates.get("enabled") is not None:
            row.enabled = updates["enabled"]
            agent_config.write_skill_state(_tenant_root(tenant_id), name, updates["enabled"])
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="skill.update", detail=json.dumps({"name": name, "fields": sorted(updates)})))
        session.commit()
    return {"name": name, "enabled": row.enabled}


@app.delete("/admin/tenants/{tenant_id}/skills/{name}", tags=["agent-config"])
def delete_skill(tenant_id: str, name: str, request: Request) -> dict:
    _require_agent_config_admin(request, tenant_id)
    with _SESSION_FACTORY() as session:
        _require_configurable_tenant(session, tenant_id)
        row = session.execute(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="skill not found")
        agent_config.remove_skill(_tenant_root(tenant_id), name)
        session.delete(row)
        session.add(AuditLog(tenant_id=tenant_id, actor="tenant-admin", action="skill.delete", detail=json.dumps({"name": name})))
        session.commit()
    return {"name": name, "deleted": True}


@app.post("/admin/tenants/{tenant_id}/token", tags=["auth"])
def issue_tenant_token(tenant_id: str, body: TokenRequest, request: Request) -> dict:
    _check_admin_key(request)
    with _SESSION_FACTORY() as session:
        row = session.get(Tenant, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        if row.status != "active":
            raise HTTPException(status_code=409, detail="tenant is disabled")
        extra_claims: dict[str, str] = {}
        role = "admin"  # tenant-level tokens are the tenant's master identity
        if body.user is not None:
            # User-scoped tokens are only minted for registered users — the
            # registry is the authority the management plane sells.
            registered = session.execute(
                select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.username == body.user)
            ).scalar_one_or_none()
            if registered is None:
                raise HTTPException(status_code=404, detail=f"user not found: {body.user}")
            extra_claims[USER_CLAIM] = body.user
            role = registered.role
            extra_claims[ROLE_CLAIM] = role
        token = issue_jwt(
            subject=body.subject,
            tenant_id=tenant_id,
            signing_key=_PRIVATE_PEM,
            kid=_KID,
            expires_in_seconds=body.expires_in_seconds,
            issuer="df-multitenant/tenant-admin",
            extra_claims=extra_claims or None,
        )
        session.add(AuditLog(tenant_id=tenant_id, actor=body.subject, action="token.issue", detail=json.dumps({"ttl": body.expires_in_seconds, "user": body.user, "role": role})))
        session.commit()
    return {"access_token": token, "token_type": "Bearer", "expires_in": body.expires_in_seconds, "role": role}

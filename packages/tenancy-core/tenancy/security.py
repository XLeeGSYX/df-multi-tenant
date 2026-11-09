"""JWT verification for tenant identity (design doc §2.2, decision D5).

The JWT ``tenant`` claim is the source of truth for tenant identity. The
``x-tenant-id`` header is only a consistency check (enforced by the
middleware, not here).

Two verification modes:

- ``rs256`` (default): tenant-admin signs with an RSA keypair and publishes
  ``/.well-known/jwks.json``. The verifier fetches the JWKS (cached per
  ``kid``, re-fetched on unknown kid so rotation propagates) and verifies
  the signature with PyJWT.
- ``hs256``: shared-secret signing for local demos without tenant-admin.

Mode and parameters come from environment variables so the same package
serves every service:

- ``TENANT_JWT_ALG``            — ``rs256`` (default) or ``hs256``
- ``TENANT_JWT_SECRET``         — HS256 shared secret (required in hs256 mode)
- ``TENANT_JWKS_URL``           — JWKS endpoint (required in rs256 mode)
- ``TENANT_JWT_ISSUER``         — optional ``iss`` claim check
- ``TENANT_JWT_AUDIENCE``       — optional ``aud`` claim check
- ``TENANT_JWT_LEEWAY_SECONDS`` — clock-skew leeway (default 30)
"""

from __future__ import annotations

import os
import threading
from typing import Any

import jwt

TENANT_CLAIM = "tenant"
# Optional claim naming a user WITHIN the tenant (management-plane user
# registry). Absent on tenant-level tokens.
USER_CLAIM = "user"
# Optional claim carrying the user's role ("admin" | "user"). Absent on
# tenant-level tokens (which are admin-capable by definition — see
# tenancy.context.is_admin_role).
ROLE_CLAIM = "role"

_DEFAULT_LEEWAY_SECONDS = 30


class _JwksCache:
    """Minimal thread-safe JWKS cache: ``kid -> PyJWK``.

    Kept deliberately small and dependency-light. A known kid is served from
    cache; an unknown (or missing) kid triggers exactly one re-fetch, which
    is also how key rotation propagates without a TTL timer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}

    def get_key(self, jwks_url: str, kid: str | None) -> Any:
        with self._lock:
            if kid is not None and kid in self._keys:
                return self._keys[kid]
            self._refresh(jwks_url)
            if kid is None:
                if len(self._keys) == 1:
                    return next(iter(self._keys.values()))
                raise jwt.PyJWTError("token has no kid and JWKS holds multiple keys")
            if kid not in self._keys:
                raise jwt.PyJWTError(f"unknown kid: {kid}")
            return self._keys[kid]

    def _refresh(self, jwks_url: str) -> None:
        import httpx

        response = httpx.get(jwks_url, timeout=5.0, follow_redirects=True)
        response.raise_for_status()
        document = response.json()
        keys: dict[str, Any] = {}
        for jwk_data in document.get("keys", []):
            kid = jwk_data.get("kid")
            if kid:
                keys[kid] = jwt.PyJWK(jwk_data)
        if not keys:
            raise jwt.PyJWTError(f"JWKS at {jwks_url} contains no keys")
        self._keys = keys


_JWKS_CACHE = _JwksCache()


def verify_jwt(authorization: str | None) -> dict[str, Any] | None:
    """Verify a ``Bearer`` authorization header value.

    Returns the claim dict on success, ``None`` on any verification failure
    (missing/malformed header, bad signature, expiry, unknown kid, ...).
    Callers treat ``None`` as 401 — never raise.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None

    alg = os.environ.get("TENANT_JWT_ALG", "rs256").lower()
    leeway = int(os.environ.get("TENANT_JWT_LEEWAY_SECONDS", str(_DEFAULT_LEEWAY_SECONDS)))
    options: dict[str, Any] = {"require": ["exp", "sub", TENANT_CLAIM]}
    verify_kwargs: dict[str, Any] = {"options": options, "leeway": leeway}

    issuer = os.environ.get("TENANT_JWT_ISSUER")
    if issuer:
        verify_kwargs["issuer"] = issuer
    audience = os.environ.get("TENANT_JWT_AUDIENCE")
    if audience:
        verify_kwargs["audience"] = audience

    try:
        if alg == "hs256":
            secret = os.environ.get("TENANT_JWT_SECRET")
            if not secret:
                return None
            return jwt.decode(token, secret, algorithms=["HS256"], **verify_kwargs)
        if alg == "rs256":
            jwks_url = os.environ.get("TENANT_JWKS_URL")
            if not jwks_url:
                return None
            header = jwt.get_unverified_header(token)
            key = _JWKS_CACHE.get_key(jwks_url, header.get("kid"))
            return jwt.decode(token, key.key, algorithms=["RS256"], **verify_kwargs)
        return None
    except Exception:
        # Any failure (network, signature, expiry, malformed) is a plain 401.
        return None


def extract_tenant_claim(claims: dict[str, Any]) -> str | None:
    """Pull the tenant id out of a verified claim dict (claim name per D5)."""
    value = claims.get(TENANT_CLAIM)
    return value if isinstance(value, str) else None


def extract_user_claim(claims: dict[str, Any]) -> str | None:
    """Optional user-within-tenant claim; ``None`` on tenant-level tokens."""
    value = claims.get(USER_CLAIM)
    return value if isinstance(value, str) and value else None


def extract_role_claim(claims: dict[str, Any]) -> str | None:
    """Optional role claim ("admin" | "user"); anything else reads as absent.

    An unknown value degrades to tenant-level semantics on purpose: the claim
    is signed by tenant-admin, which only ever writes the two known values —
    a foreign value means the token did not come from the registry path.
    """
    value = claims.get(ROLE_CLAIM)
    return value if value in ("admin", "user") else None


def issue_jwt(
    *,
    subject: str,
    tenant_id: str,
    secret: str | None = None,
    signing_key: Any = None,
    expires_in_seconds: int = 3600,
    extra_claims: dict[str, Any] | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    kid: str | None = None,
) -> str:
    """Issue a tenant-bearing JWT (used by tenant-admin; HS256 or RS256).

    Exactly one of ``secret`` (HS256) or ``signing_key`` (RS256 — a PEM
    string or a key object) must be provided. ``kid`` names the RS256 key in
    the token header so verifiers can select it from the JWKS. Kept here so
    signing and verification share the claim schema.
    """
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        TENANT_CLAIM: tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=expires_in_seconds)).timestamp()),
    }
    if issuer:
        payload["iss"] = issuer
    if audience:
        payload["aud"] = audience
    if extra_claims:
        payload.update(extra_claims)

    if secret is not None and signing_key is None:
        return jwt.encode(payload, secret, algorithm="HS256")
    if signing_key is not None and secret is None:
        key = getattr(signing_key, "key", signing_key)
        effective_kid = kid or getattr(signing_key, "kid", None)
        headers = {"kid": effective_kid} if effective_kid else None
        return jwt.encode(payload, key, algorithm="RS256", headers=headers)
    raise ValueError("provide exactly one of secret= (HS256) or signing_key= (RS256)")

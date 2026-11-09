#!/usr/bin/env bash
# Auth + isolation smoke test against a running tenant-admin + agent-runtime
# pair (design doc §11 acceptance flow).
#
#   make admin   &   # port 8010
#   make runtime &   # port 8001
#   make smoke
#
# Verifies:
#   1. tenant-admin health + tenant provisioning
#   2. JWT issuance carrying the tenant claim
#   3. runtime rejects unauthenticated /api/skills (401)
#   4. runtime accepts the tenant JWT (200)
#   5. forged x-tenant-id header is rejected (403)
#   6. runtime /health stays public (200)

set -euo pipefail

PORT_ADMIN="${1:-8010}"
PORT_RUNTIME="${2:-8001}"
ADMIN="http://127.0.0.1:${PORT_ADMIN}"
RUNTIME="http://127.0.0.1:${PORT_RUNTIME}"
TENANT="${SMOKE_TENANT:-smoke-$(date +%s)}"

# Optional shared-secret for the admin plane (TENANT_ADMIN_API_KEY)
ADMIN_KEY_HEADER=()
if [[ -n "${TENANT_ADMIN_API_KEY:-}" ]]; then
  ADMIN_KEY_HEADER=(-H "x-admin-key: ${TENANT_ADMIN_API_KEY}")
fi

say()  { printf '\033[1;34m[smoke]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[smoke] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

expect_status() { # expect_status <expected> <desc> <curl args...>
  local expected="$1" desc="$2"; shift 2
  local status
  status="$(curl -s -o /dev/null -w '%{http_code}' "$@")"
  [[ "$status" == "$expected" ]] || fail "$desc: expected HTTP $expected, got $status"
  say "$desc: HTTP $status ✓"
}

say "tenant-admin at $ADMIN, agent-runtime at $RUNTIME, tenant=$TENANT"

# 1. admin health + JWKS
expect_status 200 "admin /healthz" "$ADMIN/healthz"
expect_status 200 "admin JWKS endpoint" "$ADMIN/.well-known/jwks.json"

# 2. provision the smoke tenant
expect_status 201 "create tenant" -X POST \
  ${ADMIN_KEY_HEADER[@]+"${ADMIN_KEY_HEADER[@]}"} -H 'content-type: application/json' \
  -d "{\"id\": \"${TENANT}\", \"name\": \"Smoke Tenant\"}" \
  "$ADMIN/admin/tenants"

# 3. issue a JWT carrying the tenant claim
TOKEN_JSON="$(curl -s -X POST ${ADMIN_KEY_HEADER[@]+"${ADMIN_KEY_HEADER[@]}"} \
  -H 'content-type: application/json' -d '{}' \
  "$ADMIN/admin/tenants/${TENANT}/token")"
TOKEN="$(printf '%s' "$TOKEN_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[[ -n "$TOKEN" ]] || fail "no access_token in response: $TOKEN_JSON"
say "issued tenant JWT (${#TOKEN} chars)"

# 4. runtime public health (no token, no tenant)
expect_status 200 "runtime /health is public" "$RUNTIME/health"

# 5. runtime rejects unauthenticated API access
expect_status 401 "runtime /api/skills without token" "$RUNTIME/api/skills"

# 6. runtime accepts the tenant JWT
expect_status 200 "runtime /api/skills with tenant JWT" \
  -H "authorization: Bearer ${TOKEN}" "$RUNTIME/api/skills"

# 7. forged x-tenant-id is rejected even with a valid JWT
expect_status 403 "runtime /api/skills with mismatched x-tenant-id" \
  -H "authorization: Bearer ${TOKEN}" -H "x-tenant-id: some-other-tenant" \
  "$RUNTIME/api/skills"

# 8. cleanup (best effort)
curl -s -o /dev/null -X DELETE ${ADMIN_KEY_HEADER[@]+"${ADMIN_KEY_HEADER[@]}"} \
  "$ADMIN/admin/tenants/${TENANT}" || true

say "ALL CHECKS PASSED"

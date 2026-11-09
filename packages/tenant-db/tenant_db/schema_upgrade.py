"""Additive sqlite schema upgrades for pre-existing tenant databases.

``Base.metadata.create_all`` only creates *missing* tables — it never adds
columns to existing ones, so a database created before the ``role`` column
keep silently missing it after an upgrade. The demo stack runs sqlite (no
Alembic for these management-plane tables), so the upgrade is a tiny
``PRAGMA table_info`` check plus ``ALTER TABLE ... ADD COLUMN`` with the same
default the ORM model declares.

Every engine-creation site must call :func:`ensure_schema` right after
``create_all`` (see ``apps/tenant_admin/main.py`` and the test suites). New
columns append to ``_ADDED_COLUMNS`` — never reorder or edit settled entries,
the DDL must keep matching what fresh databases already contain.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# table -> column -> ALTER TABLE column DDL (type + constraints + default).
# Must stay in lockstep with the column definitions in tenant_db.models.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tenant_users": {
        "role": "VARCHAR(16) NOT NULL DEFAULT 'user'",
    },
}


def ensure_schema(engine: Engine) -> None:
    """Add any missing ``_ADDED_COLUMNS`` columns (idempotent, sqlite-only)."""
    if engine.dialect.name != "sqlite":
        # Other dialects go through a real migration tool; do nothing rather
        # than guess their ALTER syntax.
        return
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in _inspect_columns(conn, table)}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _inspect_columns(conn: Any, table: str) -> list[tuple]:
    return list(conn.execute(text(f"PRAGMA table_info({table})")))

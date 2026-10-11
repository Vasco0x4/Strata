"""Agent tokens: generated once, shown once, stored as a SHA-256 hash."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass

from .config import Quota
from .db import Database
from .errors import Conflict, NotFound, StrataError

TOKEN_PREFIX = "strata_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Caller:
    id: int
    name: str
    is_admin: bool
    max_vms: int
    max_ram_mb: int
    max_vcpus: int


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_agent(db: Database, name: str, quota: Quota, *, is_admin: bool = False,
                 expires_in_days: float | None = None) -> str:
    """Create an agent and return its token. The token is not stored and cannot be shown again."""
    if not NAME_RE.fullmatch(name):
        raise StrataError(f"invalid agent name {name!r}",
                          "use 1-32 lowercase letters, digits, '-' or '_'")
    if db.one("SELECT 1 FROM agents WHERE name = ?", (name,)):
        raise Conflict(f"agent {name!r} already exists")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO agents (name, token_hash, is_admin, max_vms, max_ram_mb, max_vcpus,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, hash_token(token), int(is_admin), quota.max_vms, quota.max_ram_mb,
         quota.max_vcpus, now, now + expires_in_days * 86400 if expires_in_days else None),
    )
    return token


def revoke_agent(db: Database, name: str) -> None:
    cur = db.execute("UPDATE agents SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                     (time.time(), name))
    if cur.rowcount == 0:
        raise NotFound(f"no active agent named {name!r}")


def authenticate(db: Database, token: str | None) -> Caller | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = db.one("SELECT * FROM agents WHERE token_hash = ? AND revoked_at IS NULL",
                 (hash_token(token),))
    if row is None or (row["expires_at"] and row["expires_at"] < time.time()):
        return None
    return Caller(row["id"], row["name"], bool(row["is_admin"]), row["max_vms"],
                  row["max_ram_mb"], row["max_vcpus"])


def list_agents(db: Database) -> list[dict]:
    return db.all("SELECT id, name, is_admin, max_vms, max_ram_mb, max_vcpus, created_at,"
                  " expires_at, revoked_at FROM agents ORDER BY created_at")

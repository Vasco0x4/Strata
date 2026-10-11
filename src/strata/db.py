"""SQLite storage: agents and their tokens, VMs, snapshots, journal."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    token_hash  TEXT NOT NULL UNIQUE,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    max_vms     INTEGER NOT NULL,
    max_ram_mb  INTEGER NOT NULL,
    max_vcpus   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    revoked_at  REAL
);

CREATE TABLE IF NOT EXISTS images (
    name        TEXT PRIMARY KEY,
    os          TEXT NOT NULL CHECK (os IN ('windows', 'linux')),
    volume      TEXT NOT NULL,
    username    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vms (
    name        TEXT PRIMARY KEY,
    owner_id    INTEGER NOT NULL REFERENCES agents(id),
    os          TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('disposable', 'permanent')),
    vcpus       INTEGER NOT NULL,
    ram_mb      INTEGER NOT NULL,
    mac         TEXT NOT NULL UNIQUE,
    ip          TEXT NOT NULL UNIQUE,
    ssh_port    INTEGER NOT NULL UNIQUE,
    rdp_port    INTEGER UNIQUE,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,
    internet    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    expires_at  REAL
);

CREATE TABLE IF NOT EXISTS snapshots (
    vm          TEXT NOT NULL REFERENCES vms(name) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    with_memory INTEGER NOT NULL,
    disk        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (vm, name)
);

CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,
    vm          TEXT,
    action      TEXT NOT NULL,
    params      TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    result      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_vm ON journal (vm, ts);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)
        if self.path != ":memory:":
            Path(self.path).chmod(0o600)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def log(self, actor: str, vm: str | None, action: str, params: dict, ok: bool,
            result: Any, duration_ms: int) -> None:
        self.execute(
            "INSERT INTO journal (ts, actor, vm, action, params, ok, result, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), actor, vm, action, json.dumps(params, default=str), int(ok),
             json.dumps(result, default=str)[:20000], duration_ms),
        )

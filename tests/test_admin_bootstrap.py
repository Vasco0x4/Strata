"""ensure_admin runs at every startup and mints the admin credential. Verify it creates
the admin once, writes the token file 0600, reuses it afterwards, and that the token works.
"""

import stat
import sys
import types

import pytest


@pytest.fixture
def server_mod(monkeypatch):
    for mod in ("libvirt", "libvirt_qemu", "guestfs"):
        m = types.ModuleType(mod)
        m.__getattr__ = lambda name: 0  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, mod, m)
    import importlib
    srv = importlib.import_module("strata.server")
    return importlib.reload(srv)


def make(tmp_path):
    from strata.config import Config
    from strata.db import Database
    cfg = Config(data_dir=tmp_path)
    return cfg, Database(cfg.db_path)


def test_creates_admin_and_writes_token_once(server_mod, tmp_path):
    cfg, db = make(tmp_path)
    admin = server_mod.ensure_admin(cfg, db)
    assert admin.is_admin and admin.name == "admin"
    token_file = cfg.data_dir / "admin-token"
    assert token_file.exists()
    # 0600: only the owner (the strata service user) can read the admin token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text().strip()

    # The written token authenticates as that admin
    from strata import auth
    caller = auth.authenticate(db, token)
    assert caller is not None and caller.is_admin and caller.name == "admin"

    # Only one admin row exists
    assert db.one("SELECT COUNT(*) n FROM agents WHERE is_admin = 1")["n"] == 1


def test_second_start_reuses_admin(server_mod, tmp_path):
    cfg, db = make(tmp_path)
    first = server_mod.ensure_admin(cfg, db)
    token_after_first = (cfg.data_dir / "admin-token").read_text()
    second = server_mod.ensure_admin(cfg, db)
    assert second.id == first.id
    # No new admin, and the existing token file is left as-is
    assert db.one("SELECT COUNT(*) n FROM agents WHERE is_admin = 1")["n"] == 1
    assert (cfg.data_dir / "admin-token").read_text() == token_after_first


def test_connect_command_is_pasteable(server_mod, tmp_path):
    from strata.api import connect_command
    from strata.config import Config
    cfg = Config(public_host="10.1.2.3", port=8080)
    line = connect_command(cfg, "strata_TOKEN123")
    assert line == ('claude mcp add --transport http strata http://10.1.2.3:8080/mcp '
                    '--header "Authorization: Bearer strata_TOKEN123"')

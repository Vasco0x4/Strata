"""End-to-end: the real HTTP server and the real MCP client, on the fake hypervisor."""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from conftest import FakeHypervisor, FakeNet
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from strata import auth
from strata.config import Config, Quota
from strata.db import Database
from strata.server import build_app
from strata.service import Service


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = free_port()
    cfg = Config(public_host="127.0.0.1", port=port, listen="127.0.0.1")
    db = Database(":memory:")
    db.execute("INSERT INTO images (name, os, volume, username, created_at)"
               " VALUES ('ubuntu', 'linux', 'ubuntu.qcow2', 'strata', 0)")
    hv = FakeHypervisor()
    svc = Service(cfg, db, hv, FakeNet())
    app = build_app(cfg, db, svc, hv)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    tokens = {
        "admin": auth.create_agent(db, "admin", Quota(), is_admin=True),
        "alice": auth.create_agent(db, "alice", Quota()),
        "bob": auth.create_agent(db, "bob", Quota()),
    }
    yield cfg, db, tokens
    srv.should_exit = True
    thread.join(5)


async def mcp_session(cfg, token):
    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"} if token else None)
    return client, streamable_http_client(f"{cfg.base_url}/mcp", http_client=client)


def call(cfg, token, steps):
    """Open an MCP session with this token and run steps(session) inside it."""
    async def go():
        client, transport = await mcp_session(cfg, token)
        async with client, transport as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await steps(session)
    return asyncio.run(go())


def test_mcp_rejects_missing_or_bad_token(server):
    cfg, db, tokens = server
    for token in (None, "strata_wrong"):
        r = httpx.post(f"{cfg.base_url}/mcp", headers={"Authorization": f"Bearer {token}"} if token else {},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r.status_code == 401


def test_mcp_tools_end_to_end(server):
    cfg, db, tokens = server

    async def steps(s):
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"create_vm", "exec", "snapshot", "revert", "fork", "screenshot", "destroy"} <= names
        created = await s.call_tool("create_vm", {"image": "ubuntu", "name": "e2e"})
        assert not created.is_error, created.content
        out = await s.call_tool("exec", {"vm": "e2e", "command": "uname -a"})
        assert not out.is_error and "uname -a" in out.content[0].text
        assert not (await s.call_tool("snapshot", {"vm": "e2e", "name": "clean"})).is_error
        assert not (await s.call_tool("revert", {"vm": "e2e", "snapshot": "clean"})).is_error
        assert not (await s.call_tool("fork", {"vm": "e2e", "snapshot": "clean", "name": "e2e-fork"})).is_error
        shot = await s.call_tool("screenshot", {"vm": "e2e"})
        assert shot.content[0].type == "image" and shot.content[0].mime_type == "image/png"
        up = await s.call_tool("upload", {"vm": "e2e", "path": "/tmp/a.bin", "content_base64": "TVqQAA=="})
        assert not up.is_error
        down = await s.call_tool("download", {"vm": "e2e", "path": "/tmp/a.bin"})
        assert "TVqQAA==" in down.content[0].text
        missing = await s.call_tool("exec", {"vm": "nope", "command": "id"})
        assert missing.is_error and "no VM named 'nope'" in missing.content[0].text
    call(cfg, tokens["alice"], steps)

    async def bob_steps(s):
        listed = await s.call_tool("list_vms", {})
        assert "e2e" not in str(listed.content)
        res = await s.call_tool("destroy", {"vm": "e2e"})
        assert res.is_error
    call(cfg, tokens["bob"], bob_steps)


def test_revoked_token_stops_working(server):
    cfg, db, tokens = server
    token = auth.create_agent(db, "temp", Quota())
    call(cfg, token, lambda s: s.list_tools())
    auth.revoke_agent(db, "temp")
    r = httpx.post(f"{cfg.base_url}/mcp", headers={"Authorization": f"Bearer {token}"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401


def test_rest_api_and_web_login(server):
    cfg, db, tokens = server
    base = cfg.base_url
    # bearer token
    r = httpx.get(f"{base}/api/me", headers={"Authorization": f"Bearer {tokens['alice']}"})
    assert r.json()["name"] == "alice"
    assert httpx.get(f"{base}/api/me").status_code == 401
    # agents are admin-only
    assert httpx.get(f"{base}/api/agents", headers={"Authorization": f"Bearer {tokens['alice']}"}).status_code == 403
    r = httpx.post(f"{base}/api/agents", headers={"Authorization": f"Bearer {tokens['admin']}"},
                   json={"name": "carol", "max_vms": 1})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["token"].startswith("strata_") and new["token"] in new["connect"]
    assert "claude mcp add --transport http strata" in new["connect"]

    # browser login with a cookie; writes need the X-Strata header
    with httpx.Client(base_url=base) as browser:
        assert browser.post("/api/login", json={"token": "strata_bad"}).status_code == 401
        assert browser.post("/api/login", json={"token": new["token"]}).status_code == 200
        assert browser.get("/api/me").json()["name"] == "carol"
        r = browser.post("/api/vms", json={"image": "ubuntu", "name": "c1"})
        assert r.status_code == 403 and "X-Strata" in r.text
        r = browser.post("/api/vms", json={"image": "ubuntu", "name": "c1"}, headers={"X-Strata": "1"})
        assert r.status_code == 200, r.text
        r = browser.post("/api/vms", json={"image": "ubuntu"}, headers={"X-Strata": "1"})
        assert r.status_code == 403 and "quota" in r.json()["error"]
        assert browser.get("/").status_code == 200

    # files over HTTP
    h = {"Authorization": f"Bearer {tokens['admin']}"}
    r = httpx.put(f"{base}/api/vms/c1/files", params={"path": "/tmp/x"}, content=b"\x00\x01binary", headers=h)
    assert r.status_code == 200, r.text
    assert httpx.get(f"{base}/api/vms/c1/files", params={"path": "/tmp/x"}, headers=h).content == b"\x00\x01binary"


def test_cli(server, monkeypatch, capsys):
    from strata import cli
    cfg, db, tokens = server
    monkeypatch.setenv("STRATA_URL", cfg.base_url)
    monkeypatch.setenv("STRATA_TOKEN", tokens["bob"])

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["strata", *argv])
        try:
            cli.main()
        except SystemExit as e:
            assert not e.code, capsys.readouterr()
        return capsys.readouterr().out

    run("create", "ubuntu", "--name", "cli1")
    assert "cli1" in run("vms")
    assert "echo hi" in run("exec", "cli1", "--", "echo", "hi")
    run("snapshot", "cli1", "s1")
    assert "s1" in run("snapshots", "cli1")
    assert "access" in run("--json", "show", "cli1")
    run("destroy", "-y", "cli1")
    assert "cli1" not in run("vms")


def test_image_registration_allows_dotted_names(server):
    cfg, db, tokens = server
    base = cfg.base_url
    h = {"Authorization": f"Bearer {tokens['admin']}", "X-Strata": "1"}
    # a dotted name like ubuntu-24.04 is valid for an image
    r = httpx.post(f"{base}/api/images", headers=h,
                   json={"name": "ubuntu-24.04", "os": "linux", "volume": "ubuntu-24.04.qcow2",
                         "username": "strata", "description": "Ubuntu 24.04"})
    assert r.status_code == 200, r.text
    assert r.json()["image"] == "ubuntu-24.04"
    assert any(i["name"] == "ubuntu-24.04" for i in httpx.get(f"{base}/api/images", headers=h).json())
    # but a traversal-ish or spaced name is still rejected
    for bad in ("../etc", "bad name", "UPPER"):
        rr = httpx.post(f"{base}/api/images", headers=h,
                        json={"name": bad, "os": "linux", "volume": "x.qcow2", "username": "s"})
        assert rr.status_code == 400, f"{bad!r} should be rejected"
    # non-admins cannot register images
    assert httpx.post(f"{base}/api/images", headers={"Authorization": f"Bearer {tokens['alice']}", "X-Strata": "1"},
                      json={"name": "x", "os": "linux", "volume": "x.qcow2", "username": "s"}).status_code == 403

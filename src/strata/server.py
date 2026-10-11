"""stratad: the Strata service.

Serves on the LAN (MCP for agents, HTTP API for the CLI, web UI for humans) and on a
local Unix socket for host administration (group strata-admin, acts as the admin).
"""

from __future__ import annotations

import asyncio
import grp
import logging
import os
import socket
import threading
import time

import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from . import __version__, auth, config, mcp_tools
from .api import Api
from .db import Database
from .service import Service

log = logging.getLogger("strata")
ADMIN_GROUP = "strata-admin"


class TokenVerifier:
    def __init__(self, db: Database):
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        caller = auth.authenticate(self.db, token)
        if caller is None:
            return None
        return AccessToken(token=token, client_id=caller.name, scopes=[])


def ensure_admin(cfg: config.Config, db: Database) -> auth.Caller:
    """The first start creates the admin agent and writes its token for the host owner."""
    row = db.one("SELECT name FROM agents WHERE is_admin = 1 AND revoked_at IS NULL ORDER BY id LIMIT 1")
    if row is None:
        name = "admin" if not db.one("SELECT 1 FROM agents WHERE name = 'admin'") else f"admin-{int(time.time())}"
        token = auth.create_agent(db, name, cfg.default_quota, is_admin=True)
        path = cfg.data_dir / "admin-token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        log.info("admin token written to %s", path)
        row = {"name": name}
    r = db.one("SELECT * FROM agents WHERE name = ?", (row["name"],))
    return auth.Caller(r["id"], r["name"], True, r["max_vms"], r["max_ram_mb"], r["max_vcpus"])


def build_app(cfg: config.Config, db: Database, svc: Service, hv) -> Starlette:
    mcp = MCPServer(
        name="strata", title="Strata", version=__version__, instructions=mcp_tools.INSTRUCTIONS,
        token_verifier=TokenVerifier(db),
        auth=AuthSettings(issuer_url=cfg.base_url, resource_server_url=f"{cfg.base_url}/mcp",
                          validate_token_resource=False),  # opaque tokens, checked against the DB
    )
    mcp_tools.register(mcp, svc, db)
    hosts = [cfg.public_host, f"{cfg.public_host}:{cfg.port}", "127.0.0.1:*", "localhost:*"]
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts, allowed_origins=[f"http://{h}" for h in hosts]),
        max_request_body_size=48 * 1024 * 1024,
        host=cfg.listen,
    )
    api = Api(cfg, db, svc, hv)
    app.router.routes.extend(api.routes() + api.web_routes())
    return app


def build_admin_app(cfg: config.Config, db: Database, svc: Service, hv, admin: auth.Caller) -> Starlette:
    api = Api(cfg, db, svc, hv)
    app = Starlette(routes=api.routes())

    async def as_admin(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope["strata.caller"] = admin
        await app(scope, receive, send)
    return as_admin


def admin_socket(cfg: config.Config) -> socket.socket:
    """Unix socket for host administration, owned by group strata-admin, mode 0660."""
    path = str(cfg.admin_socket)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    try:
        os.chown(path, -1, grp.getgrnam(ADMIN_GROUP).gr_gid)
        os.chmod(path, 0o660)
    except (KeyError, PermissionError):
        log.warning("group %s not available, admin socket restricted to its owner", ADMIN_GROUP)
    sock.listen(64)
    return sock


def housekeeping(svc: Service, stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            for name in svc.reap_expired():
                log.info("destroyed expired VM %s", name)
        except Exception:
            log.exception("housekeeping")


async def serve(cfg, app, admin_app) -> None:
    lan = uvicorn.Server(uvicorn.Config(app, host=cfg.listen, port=cfg.port, log_level="info",
                                        proxy_headers=False, server_header=False))
    local = uvicorn.Server(uvicorn.Config(admin_app, log_level="warning", lifespan="off"))
    await asyncio.gather(lan.serve(), local.serve(sockets=[admin_socket(cfg)]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from .libvirt_hv import LibvirtHypervisor
    from .netctl import NetctlClient

    cfg = config.load()
    db = Database(cfg.db_path)
    hv = LibvirtHypervisor(cfg)
    svc = Service(cfg, db, hv, NetctlClient(cfg.netctl_socket))
    admin = ensure_admin(cfg, db)
    try:
        svc.resync()
    except Exception:
        log.exception("could not restore firewall state at startup")
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(svc, stop), daemon=True).start()
    log.info("Strata %s on %s", __version__, cfg.base_url)
    try:
        asyncio.run(serve(cfg, build_app(cfg, db, svc, hv), build_admin_app(cfg, db, svc, hv, admin)))
    finally:
        stop.set()

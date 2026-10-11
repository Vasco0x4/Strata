"""HTTP API used by the CLI and the web UI. Same service, same permissions as MCP."""

from __future__ import annotations

import asyncio
import functools
import json
import socket
import time
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth
from .config import Config
from .db import Database
from .errors import Conflict, Forbidden, NotFound, StrataError
from .service import Service

COOKIE = "strata_token"
WEB_DIR = Path(__file__).parent / "web"
NOVNC_DIR = Path("/usr/share/novnc")
MAX_FILE_UPLOAD = 2 * 1024 * 1024 * 1024
STATUS = {NotFound: 404, Forbidden: 403, Conflict: 409}


class Unauthorized(StrataError):
    pass


def rdp_file(address: str, username: str) -> str:
    """RDP connection file with every device and clipboard redirection off: nothing crosses
    from the VM to the client machine except the screen."""
    return "\r\n".join([
        f"full address:s:{address}", f"username:s:{username}", "prompt for credentials:i:1",
        "redirectclipboard:i:0", "redirectdrives:i:0", "drivestoredirect:s:", "redirectprinters:i:0",
        "redirectcomports:i:0", "redirectsmartcards:i:0", "redirectposdevices:i:0",
        "devicestoredirect:s:", "usbdevicestoredirect:s:", "camerastoredirect:s:",
        "redirectwebauthn:i:0", "audiocapturemode:i:0", "audiomode:i:2", "authentication level:i:2",
    ]) + "\r\n"


def connect_command(cfg: Config, token: str) -> str:
    return (f'claude mcp add --transport http strata {cfg.base_url}/mcp '
            f'--header "Authorization: Bearer {token}"')


class Api:
    def __init__(self, cfg: Config, db: Database, svc: Service, hv):
        self.cfg, self.db, self.svc, self.hv = cfg, db, svc, hv

    # ---------- auth ----------

    def caller(self, request: Request) -> auth.Caller:
        local = request.scope.get("strata.caller")   # set only on the local admin socket
        if local is not None:
            return local
        token = None
        user = request.scope.get("user")
        if user is not None and getattr(user, "access_token", None) is not None:
            token = user.access_token.token
        elif cookie := request.cookies.get(COOKIE):
            # Browsers send cookies on cross-site requests; a custom header they cannot forge stops that
            if request.method not in ("GET", "HEAD") and request.headers.get("x-strata") != "1":
                raise Forbidden("missing X-Strata header")
            token = cookie
        caller = auth.authenticate(self.db, token)
        if caller is None:
            raise Unauthorized("invalid, expired or revoked token")
        return caller

    def admin(self, request: Request) -> auth.Caller:
        caller = self.caller(request)
        if not caller.is_admin:
            raise Forbidden("admin only")
        return caller

    # ---------- plumbing ----------

    @staticmethod
    def error(e: StrataError) -> JSONResponse:
        status = 401 if isinstance(e, Unauthorized) else STATUS.get(type(e), 400)
        return JSONResponse({"error": e.message, "hint": e.hint}, status_code=status)

    def endpoint(self, fn):
        """Wrap a handler: errors become JSON, blocking work runs in a thread."""
        @functools.wraps(fn)
        async def handler(request: Request):
            try:
                body = {}
                if request.method in ("POST", "PUT", "PATCH") and \
                        request.headers.get("content-type", "").startswith("application/json"):
                    body = await request.json()
                    if not isinstance(body, dict):
                        raise StrataError("JSON body must be an object")
                result = await anyio.to_thread.run_sync(functools.partial(fn, request, body))
            except StrataError as e:
                return self.error(e)
            except (json.JSONDecodeError, TypeError) as e:
                return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
            return result if isinstance(result, Response) else JSONResponse(result)
        return handler

    # ---------- routes ----------

    def routes(self) -> list:
        s, e = self.svc, self.endpoint
        p = lambda r, k: r.path_params[k]  # noqa: E731
        allowed = lambda body, *keys: {k: v for k, v in body.items() if k in keys}  # noqa: E731

        def me(r, b):
            c = self.caller(r)
            return {"name": c.name, "is_admin": c.is_admin,
                    "quota": {"max_vms": c.max_vms, "max_ram_mb": c.max_ram_mb, "max_vcpus": c.max_vcpus}}

        def create_vm(r, b):
            return s.create_vm(self.caller(r), **allowed(b, "image", "from_vm", "from_snapshot", "name",
                                                          "vcpus", "ram_mb", "permanent", "internet"))

        def vm_exec(r, b):
            return s.exec(self.caller(r), p(r, "vm"), **allowed(b, "command", "shell", "timeout"))

        def snapshots(r, b):
            if r.method == "POST":
                return s.snapshot(self.caller(r), p(r, "vm"), **allowed(b, "name", "with_memory"))
            return s.list_snapshots(self.caller(r), p(r, "vm"))

        def screenshot(r, b):
            return Response(s.screenshot(self.caller(r), p(r, "vm")), media_type="image/png",
                            headers={"Cache-Control": "no-store"})

        def download(r, b):
            data = s.download(self.caller(r), p(r, "vm"), r.query_params["path"])
            return Response(data, media_type="application/octet-stream",
                            headers={"Content-Disposition": "attachment"})

        def rdp(r, b):
            info = s.get_vm(self.caller(r), p(r, "vm"))
            if not info["access"]["rdp"]:
                raise StrataError(f"{info['name']} has no RDP (not a Windows VM)")
            return Response(rdp_file(info["access"]["rdp"], info["access"]["username"]),
                            media_type="application/x-rdp",
                            headers={"Content-Disposition": f'attachment; filename="{info["name"]}.rdp"'})

        def agents(r, b):
            self.admin(r)
            if r.method == "POST":
                quota = self.cfg.default_quota.__class__(**{
                    k: int(b.get(k, getattr(self.cfg.default_quota, k)))
                    for k in ("max_vms", "max_ram_mb", "max_vcpus")})
                token = auth.create_agent(self.db, b.get("name", ""), quota,
                                          is_admin=bool(b.get("is_admin")),
                                          expires_in_days=b.get("expires_in_days"))
                return {"name": b["name"], "token": token, "connect": connect_command(self.cfg, token)}
            return auth.list_agents(self.db)

        def revoke(r, b):
            self.admin(r)
            auth.revoke_agent(self.db, p(r, "name"))
            return {"revoked": p(r, "name")}

        def images(r, b):
            if r.method == "POST":
                self.admin(r)
                return self.register_image(**allowed(b, "name", "os", "volume", "username", "description"))
            return s.list_images(self.caller(r))

        def login(r, b):
            token = b.get("token", "")
            caller = auth.authenticate(self.db, token)
            if caller is None:
                raise Unauthorized("invalid, expired or revoked token")
            resp = JSONResponse({"name": caller.name, "is_admin": caller.is_admin})
            resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict", path="/")
            return resp

        def logout(r, b):
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(COOKIE, path="/")
            return resp

        vm = "/api/vms/{vm}"
        snap = vm + "/snapshots/{snap}"
        routes = [
            Route("/api/login", e(login), methods=["POST"]),
            Route("/api/logout", e(logout), methods=["POST"]),
            Route("/api/me", e(me)),
            Route("/api/images", e(images), methods=["GET", "POST"]),
            Route("/api/agents", e(agents), methods=["GET", "POST"]),
            Route("/api/agents/{name}", e(revoke), methods=["DELETE"]),
            Route("/api/history", e(lambda r, b: s.history(
                self.caller(r), vm=r.query_params.get("vm"), limit=int(r.query_params.get("limit", 50))))),
            Route("/api/vms", e(lambda r, b: create_vm(r, b) if r.method == "POST" else s.list_vms(self.caller(r))),
                  methods=["GET", "POST"]),
            Route(vm, e(lambda r, b: s.destroy(self.caller(r), p(r, "vm")) if r.method == "DELETE"
                        else s.get_vm(self.caller(r), p(r, "vm"))), methods=["GET", "DELETE"]),
            Route(vm + "/exec", e(vm_exec), methods=["POST"]),
            Route(vm + "/files", self.files, methods=["GET", "PUT"]),
            Route(vm + "/screenshot", e(screenshot)),
            Route(vm + "/click", e(lambda r, b: s.click(self.caller(r), p(r, "vm"),
                                                        **allowed(b, "x", "y", "button", "double"))),
                  methods=["POST"]),
            Route(vm + "/type", e(lambda r, b: s.type_text(self.caller(r), p(r, "vm"), b.get("text", ""))),
                  methods=["POST"]),
            Route(vm + "/keys", e(lambda r, b: s.send_keys(self.caller(r), p(r, "vm"), list(b.get("keys", [])))),
                  methods=["POST"]),
            Route(vm + "/power", e(lambda r, b: s.power(self.caller(r), p(r, "vm"), b.get("action", ""))),
                  methods=["POST"]),
            Route(vm + "/keep", e(lambda r, b: s.keep(self.caller(r), p(r, "vm"), **allowed(b, "hours", "permanent"))),
                  methods=["POST"]),
            Route(vm + "/internet", e(lambda r, b: s.set_internet(self.caller(r), p(r, "vm"), bool(b.get("enabled")))),
                  methods=["POST"]),
            Route(vm + "/snapshots", e(snapshots), methods=["GET", "POST"]),
            Route(snap, e(lambda r, b: s.delete_snapshot(self.caller(r), p(r, "vm"), p(r, "snap"))),
                  methods=["DELETE"]),
            Route(snap + "/revert", e(lambda r, b: s.revert(self.caller(r), p(r, "vm"), p(r, "snap"))),
                  methods=["POST"]),
            Route(snap + "/fork", e(lambda r, b: s.fork(self.caller(r), p(r, "vm"), p(r, "snap"),
                                                        **allowed(b, "name", "internet"))), methods=["POST"]),
            Route(vm + "/download", e(download)),
            Route(vm + "/rdp", e(rdp)),
            WebSocketRoute(vm + "/console", self.console),
        ]
        return routes

    def web_routes(self) -> list:
        index = lambda r: FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})  # noqa: E731
        routes = [Route("/", index), Route("/vm/{vm}", index), Route("/agents", index),
                  Mount("/static", StaticFiles(directory=WEB_DIR))]
        if NOVNC_DIR.is_dir():
            routes.append(Mount("/novnc", StaticFiles(directory=NOVNC_DIR)))
        return routes

    # ---------- handlers that need more than JSON ----------

    async def files(self, request: Request):
        """PUT raw bytes to /files?path=... to upload (up to 2 GB); GET to download."""
        try:
            caller = await anyio.to_thread.run_sync(self.caller, request)
            vm, path = request.path_params["vm"], request.query_params.get("path")
            if not path:
                raise StrataError("missing ?path=")
            if request.method == "GET":
                data = await anyio.to_thread.run_sync(self.svc.download, caller, vm, path)
                return Response(data, media_type="application/octet-stream")
            buf = bytearray()
            async for chunk in request.stream():
                buf += chunk
                if len(buf) > MAX_FILE_UPLOAD:
                    raise StrataError("file larger than 2 GB")
            result = await anyio.to_thread.run_sync(self.svc.upload, caller, vm, path, bytes(buf))
            return JSONResponse(result)
        except StrataError as e:
            return self.error(e)

    def register_image(self, name: str, os: str, volume: str, username: str, description: str = "") -> dict:
        if os not in ("windows", "linux"):
            raise StrataError("os must be windows or linux")
        if not auth.NAME_RE.fullmatch(name):
            raise StrataError(f"invalid image name {name!r}")
        self.hv.volume_path(self.cfg.images_pool, volume)  # must exist
        self.db.execute("INSERT INTO images (name, os, volume, username, description, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET os = excluded.os,"
                        " volume = excluded.volume, username = excluded.username,"
                        " description = excluded.description",
                        (name, os, volume, username, description, time.time()))
        return {"image": name, "volume": volume}

    async def console(self, ws: WebSocket):
        """Live screen for the web UI: a WebSocket carrying the VM's VNC stream.

        The VM's display has no listening socket; libvirt hands us a connected one.
        """
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if origin and host and origin.split("://", 1)[-1] != host:
            await ws.close(code=4403)   # another site trying to use the user's cookie
            return
        try:
            caller = auth.authenticate(self.db, ws.cookies.get(COOKIE)) or ws.scope.get("strata.caller")
            if caller is None:
                raise Unauthorized("not logged in")
            vm = ws.path_params["vm"]
            await anyio.to_thread.run_sync(self.svc.get_vm, caller, vm)   # permission check
            fd = await anyio.to_thread.run_sync(self.hv.open_console, vm)
        except StrataError:
            await ws.close(code=4401)
            return
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=sock)
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol="binary" if "binary" in offered else None)

        async def browser_to_vm():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    data = msg.get("bytes") or (msg.get("text") or "").encode()
                    writer.write(data)
                    await writer.drain()
            except (WebSocketDisconnect, ConnectionError):
                return

        async def vm_to_browser():
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                return

        tasks = [asyncio.create_task(browser_to_vm()), asyncio.create_task(vm_to_browser())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await ws.close()
        except RuntimeError:
            pass

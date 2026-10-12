"""strata: command line for Strata.

On the host, members of strata-admin talk to the local admin socket with no setup.
Elsewhere: `strata login http://HOST:8080 TOKEN` once, or set STRATA_URL and STRATA_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import httpx

ADMIN_SOCKET = "/run/strata/admin.sock"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "strata" / "cli.toml"


def client() -> httpx.Client:
    url, token = os.environ.get("STRATA_URL"), os.environ.get("STRATA_TOKEN")
    if not (url and token) and CONFIG.exists():
        saved = tomllib.loads(CONFIG.read_text())
        url, token = url or saved.get("url"), token or saved.get("token")
    timeout = httpx.Timeout(30, read=1800)
    if url and token:
        return httpx.Client(base_url=url.rstrip("/"), timeout=timeout,
                            headers={"Authorization": f"Bearer {token}"})
    if os.access(ADMIN_SOCKET, os.R_OK | os.W_OK):
        return httpx.Client(base_url="http://strata", timeout=timeout,
                            transport=httpx.HTTPTransport(uds=ADMIN_SOCKET))
    sys.exit("not connected: run `strata login URL TOKEN` (or set STRATA_URL and STRATA_TOKEN)")


def request(method: str, path: str, **kw):
    with client() as c:
        try:
            r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            sys.exit(f"cannot reach Strata: {e}")
    if r.status_code >= 400:
        try:
            err = r.json()
            msg = err.get("error", r.text) + (f" ({err['hint']})" if err.get("hint") else "")
        except ValueError:
            msg = r.text
        sys.exit(f"error: {msg}")
    return r


def show(data, as_json: bool) -> None:
    if as_json or not isinstance(data, (list, dict)):
        print(json.dumps(data, indent=2) if not isinstance(data, str) else data)
        return
    rows = data if isinstance(data, list) else [data]
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for k2, v2 in v.items():
                    print(f"  {k2:<10} {v2}")
            else:
                print(f"{k:<18} {v}")
        return
    if not rows:
        print("(none)")
        return
    cols = [c for c in rows[0] if not isinstance(rows[0][c], (dict, list)) or c == "snapshots"]
    fmt = lambda v: ", ".join(v) if isinstance(v, list) else ("-" if v is None else str(v))  # noqa: E731
    widths = {c: max(len(c), *(len(fmt(r.get(c))) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(fmt(r.get(c)).ljust(widths[c]) for c in cols))


def cmd_login(a):
    r = httpx.get(f"{a.url.rstrip('/')}/api/me", headers={"Authorization": f"Bearer {a.token}"}, timeout=10)
    if r.status_code != 200:
        sys.exit("error: token refused")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f'url = "{a.url}"\ntoken = "{a.token}"\n')
    print(f"logged in as {r.json()['name']}")


def cmd_exec(a):
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        sys.exit("usage: strata exec [--shell SHELL] VM -- COMMAND")
    res = request("POST", f"/api/vms/{a.vm}/exec",
                  json={"command": " ".join(command), "shell": a.shell, "timeout": a.timeout}).json()
    if a.json:
        return show(res, True)
    sys.stdout.write(res["stdout"])
    sys.stderr.write(res["stderr"])
    if res["timed_out"]:
        sys.exit("error: timed out")
    sys.exit(res["exit_code"] or 0)


def cmd_upload(a):
    data = Path(a.local).read_bytes()
    show(request("PUT", f"/api/vms/{a.vm}/files", params={"path": a.remote}, content=data).json(), a.json)


def cmd_download(a):
    data = request("GET", f"/api/vms/{a.vm}/files", params={"path": a.remote}).content
    Path(a.local).write_bytes(data)
    print(f"{len(data)} bytes -> {a.local}")


def cmd_screenshot(a):
    out = a.output or f"{a.vm}-{time.strftime('%H%M%S')}.png"
    Path(out).write_bytes(request("GET", f"/api/vms/{a.vm}/screenshot").content)
    print(out)


def cmd_destroy(a):
    if not a.yes and input(f"destroy {a.vm} and all its snapshots? [y/N] ").lower() != "y":
        sys.exit("cancelled")
    show(request("DELETE", f"/api/vms/{a.vm}").json(), a.json)


def cmd_keep(a):
    body = {"permanent": True} if a.permanent else {"permanent": False} if a.disposable else {"hours": a.hours}
    show(request("POST", f"/api/vms/{a.vm}/keep", json=body).json(), a.json)


def simple(method, path, body=None):
    """Handler for commands that map to one request and print its JSON result."""
    def handler(a):
        p = path(a) if callable(path) else path
        b = body(a) if callable(body) else body
        show(request(method, p, **({"json": b} if b is not None else {})).json(), a.json)
    return handler


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="strata", description="Disposable and long-lived VMs.")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, handler, help, *args):
        p = sub.add_parser(name, help=help)
        for flags, kw in args:
            p.add_argument(*flags, **kw)
        p.set_defaults(func=handler)
        return p

    vm = (("vm",), {})
    snap = (("snapshot",), {})
    add("login", cmd_login, "save the URL and token to use", (("url",), {}), (("token",), {}))
    add("images", simple("GET", "/api/images"), "list images")
    add("delete-image", simple("DELETE", lambda a: f"/api/images/{a.name}"),
        "unregister an image and remove its volume (admin)", (("name",), {}))
    add("register-image", simple("POST", "/api/images", lambda a: {
        "name": a.name, "os": a.os, "volume": a.volume, "username": a.username, "description": a.description}),
        "register a volume of the images pool as an image (admin)",
        (("name",), {}), (("--os",), {"required": True, "choices": ["windows", "linux"]}),
        (("--volume",), {"required": True}), (("--username",), {"required": True}),
        (("--description",), {"default": ""}))
    add("vms", simple("GET", "/api/vms"), "list VMs")
    add("show", simple("GET", lambda a: f"/api/vms/{a.vm}"), "VM details and access", vm)
    add("create", simple("POST", "/api/vms", lambda a: {
        "image": a.image, "name": a.name, "vcpus": a.vcpus, "ram_mb": a.ram,
        "internet": a.internet, "permanent": a.permanent}), "create a VM from an image",
        (("image",), {}), (("--name",), {}), (("--vcpus",), {"type": int}), (("--ram",), {"type": int, "help": "MB"}),
        (("--internet",), {"action": "store_true"}), (("--permanent",), {"action": "store_true"}))
    add("fork", simple("POST", lambda a: f"/api/vms/{a.vm}/snapshots/{a.snapshot}/fork",
                       lambda a: {"name": a.name}), "new VM from a snapshot", vm, snap, (("--name",), {}))
    add("exec", cmd_exec, "run a command in a VM", vm, (("command",), {"nargs": argparse.REMAINDER}),
        (("--shell",), {}), (("--timeout",), {"type": float, "default": 120}))
    add("upload", cmd_upload, "copy a local file into a VM", vm, (("local",), {}), (("remote",), {}))
    add("download", cmd_download, "copy a file out of a VM", vm, (("remote",), {}), (("local",), {}))
    add("screenshot", cmd_screenshot, "save the VM's screen as PNG", vm, (("-o", "--output"), {}))
    add("snapshot", simple("POST", lambda a: f"/api/vms/{a.vm}/snapshots",
                           lambda a: {"name": a.name, "with_memory": not a.no_memory}),
        "save the VM's state", vm, (("name",), {}), (("--no-memory",), {"action": "store_true"}))
    add("snapshots", simple("GET", lambda a: f"/api/vms/{a.vm}/snapshots"), "list saved states", vm)
    add("revert", simple("POST", lambda a: f"/api/vms/{a.vm}/snapshots/{a.snapshot}/revert"),
        "go back to a saved state", vm, snap)
    add("delete-snapshot", simple("DELETE", lambda a: f"/api/vms/{a.vm}/snapshots/{a.snapshot}"),
        "forget a saved state", vm, snap)
    add("power", simple("POST", lambda a: f"/api/vms/{a.vm}/power", lambda a: {"action": a.action}),
        "start, stop, force_stop or reboot", vm,
        (("action",), {"choices": ["start", "stop", "force_stop", "reboot"]}))
    add("keep", cmd_keep, "extend expiry, or make permanent/disposable (admin)", vm,
        (("--hours",), {"type": float, "default": 24}), (("--permanent",), {"action": "store_true"}),
        (("--disposable",), {"action": "store_true"}))
    add("internet", simple("POST", lambda a: f"/api/vms/{a.vm}/internet", lambda a: {"enabled": a.state == "on"}),
        "allow or block internet for a VM", vm, (("state",), {"choices": ["on", "off"]}))
    add("destroy", cmd_destroy, "delete a VM", vm, (("-y", "--yes"), {"action": "store_true"}))
    add("diff", simple("GET", lambda a: f"/api/vms/{a.vm}/diff?from={a.snapshot_a}&to={a.snapshot_b}"),
        "what changed on disk between two snapshots", (("vm",), {}), (("snapshot_a",), {}), (("snapshot_b",), {}))
    add("history", simple("GET", lambda a: f"/api/history?limit={a.n}" + (f"&vm={a.vm}" if a.vm else "")),
        "journal of actions", (("vm",), {"nargs": "?"}), (("-n",), {"type": int, "default": 30}))
    add("agents", simple("GET", "/api/agents"), "list agents (admin)")
    add("agent-create", simple("POST", "/api/agents", lambda a: {
        "name": a.name, "is_admin": a.admin, "max_vms": a.max_vms, "max_ram_mb": a.max_ram,
        "max_vcpus": a.max_vcpus, "expires_in_days": a.days}),
        "create an agent and print its token (admin)", (("name",), {}), (("--admin",), {"action": "store_true"}),
        (("--max-vms",), {"type": int, "default": 3}), (("--max-ram",), {"type": int, "default": 16384}),
        (("--max-vcpus",), {"type": int, "default": 8}), (("--days",), {"type": float}))
    add("agent-revoke", simple("DELETE", lambda a: f"/api/agents/{a.name}"), "revoke an agent's token (admin)",
        (("name",), {}))
    return ap


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

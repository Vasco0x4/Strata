"""Everything Strata can do, with permission checks and the journal.

MCP, the HTTP API and the CLI only call this module. A caller sees and touches only its
own VMs, except the admin who sees everything. Anything coming out of a VM is untrusted
data: it is size-limited and returned as-is, never interpreted.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
import secrets
import string
import threading
import time
from collections import defaultdict
from typing import Any, Callable

from .auth import Caller
from .config import Config
from .db import Database
from .errors import Conflict, Forbidden, NotFound, StrataError
from .hypervisor import Hypervisor, NetworkControl, VMSpec

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
OS_DEFAULTS = {"windows": (4, 8192), "linux": (2, 2048)}
LIMITS = {"vcpus": (1, 16), "ram_mb": (1024, 65536)}
SHELLS = {
    "windows": {
        "powershell": lambda c: ["powershell.exe", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-Command", c],
        "cmd": lambda c: ["cmd.exe", "/c", c],
    },
    "linux": {
        "bash": lambda c: ["/bin/bash", "-lc", c],
        "sh": lambda c: ["/bin/sh", "-c", c],
    },
}
DEFAULT_SHELL = {"windows": "powershell", "linux": "bash"}
MAX_OUTPUT = 256 * 1024          # per stream, what an agent gets back from exec
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_TTL_HOURS = 24 * 7           # what a non-admin can extend a disposable VM to
READY_TIMEOUT = {"windows": 900, "linux": 300}
SSH_PORT, RDP_PORT = 22, 3389


def gen_password() -> str:
    """16 characters, always with upper, lower and digits (Windows complexity rules)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(16))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _check_name(kind: str, name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise StrataError(f"invalid {kind} name {name!r}",
                          "use 1-31 lowercase letters, digits or '-', starting with a letter or digit")
    return name


def _clip(data: bytes) -> tuple[str, bool]:
    return data[:MAX_OUTPUT].decode("utf-8", errors="replace"), len(data) > MAX_OUTPUT


def journaled(action: str, summarize: Callable[[Any], Any] | None = None, hide: tuple[str, ...] = ()):
    """Record every call: who, which VM, what, result, duration. Failures too."""
    def decorator(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(self: Service, caller: Caller, *args, **kwargs):
            bound = sig.bind(self, caller, *args, **kwargs)
            bound.apply_defaults()
            params = {k: v for k, v in bound.arguments.items()
                      if k not in ("self", "caller") and k not in hide}
            vm = params.get("vm") or params.get("name")
            start = time.monotonic()
            try:
                result = fn(self, caller, *args, **kwargs)
            except StrataError as e:
                self.db.log(caller.name, vm, action, params, False, {"error": str(e)},
                            int((time.monotonic() - start) * 1000))
                raise
            except Exception as e:
                log.exception("%s failed", action)
                self.db.log(caller.name, vm, action, params, False, {"error": f"internal: {e}"},
                            int((time.monotonic() - start) * 1000))
                raise StrataError(f"{action} failed: {e}") from e
            if action == "create_vm" and isinstance(result, dict):
                vm = result.get("name")
            self.db.log(caller.name, vm, action, params, True,
                        summarize(result) if summarize else result,
                        int((time.monotonic() - start) * 1000))
            return result
        return wrapper
    return decorator


class Service:
    def __init__(self, cfg: Config, db: Database, hv: Hypervisor, net: NetworkControl):
        self.cfg, self.db, self.hv, self.net = cfg, db, hv, net
        self._alloc_lock = threading.Lock()
        self._vm_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    # ---------- helpers ----------

    def _own(self, caller: Caller, name: str) -> dict:
        row = self.db.one("SELECT * FROM vms WHERE name = ?", (name,))
        if row is None or not (caller.is_admin or row["owner_id"] == caller.id):
            raise NotFound(f"no VM named {name!r}", "see list_vms")
        return row

    def _snap(self, vm: dict, snap: str) -> dict:
        row = self.db.one("SELECT * FROM snapshots WHERE vm = ? AND name = ?", (vm["name"], snap))
        if row is None:
            raise NotFound(f"VM {vm['name']!r} has no snapshot {snap!r}", "see list_snapshots")
        return row

    def _running(self, vm: dict) -> None:
        state = self.hv.state(vm["name"])
        if state != "running":
            raise Conflict(f"VM {vm['name']!r} is {state}", "start it with power(action='start')")

    def _allocate(self, os_: str) -> tuple[str, str, int, int | None]:
        used_ips = {r["ip"] for r in self.db.all("SELECT ip FROM vms")}
        used_macs = {r["mac"] for r in self.db.all("SELECT mac FROM vms")}
        used_ports = set()
        for r in self.db.all("SELECT ssh_port, rdp_port FROM vms"):
            used_ports.update(p for p in (r["ssh_port"], r["rdp_port"]) if p)
        ip = next((f"{self.cfg.subnet_prefix}.{i}" for i in range(10, 100)
                   if f"{self.cfg.subnet_prefix}.{i}" not in used_ips), None)
        if ip is None:
            raise Conflict("no free address left for a new VM", "destroy unused VMs")
        while (mac := "52:54:00:" + ":".join(f"{secrets.randbelow(256):02x}" for _ in range(3))) in used_macs:
            pass
        lo, hi = self.cfg.forward_ports
        free = (p for p in range(lo, hi + 1) if p not in used_ports)
        try:
            ssh_port = next(free)
            rdp_port = next(free) if os_ == "windows" else None
        except StopIteration:
            raise Conflict("no free access port left", "destroy unused VMs") from None
        return ip, mac, ssh_port, rdp_port

    def _sync_network(self) -> None:
        forwards, internet = [], []
        for r in self.db.all("SELECT ip, ssh_port, rdp_port, internet FROM vms"):
            forwards.append((r["ssh_port"], r["ip"], SSH_PORT))
            if r["rdp_port"]:
                forwards.append((r["rdp_port"], r["ip"], RDP_PORT))
            if r["internet"]:
                internet.append(r["ip"])
        self.net.sync(forwards, internet)

    def _describe(self, row: dict, *, with_secrets: bool = True) -> dict:
        host = self.cfg.public_host
        now = time.time()
        owner = self.db.one("SELECT name FROM agents WHERE id = ?", (row["owner_id"],))
        info = {
            "name": row["name"],
            "os": row["os"],
            "state": self.hv.state(row["name"]),
            "kind": row["kind"],
            "expires_in_hours": round((row["expires_at"] - now) / 3600, 1) if row["expires_at"] else None,
            "owner": owner["name"] if owner else None,
            "source": row["source"],
            "vcpus": row["vcpus"],
            "ram_mb": row["ram_mb"],
            "internet": bool(row["internet"]),
            "snapshots": [s["name"] for s in self.db.all(
                "SELECT name FROM snapshots WHERE vm = ? ORDER BY created_at", (row["name"],))],
        }
        if with_secrets:
            info["access"] = {
                "username": row["username"],
                "password": row["password"],
                "ssh": f"ssh -p {row['ssh_port']} {row['username']}@{host}",
                "rdp": f"{host}:{row['rdp_port']}" if row["rdp_port"] else None,
                "console": f"{self.cfg.base_url}/vm/{row['name']}",
            }
        return info

    # ---------- images and VMs ----------

    def list_images(self, caller: Caller) -> list[dict]:
        return self.db.all("SELECT name, os, description FROM images ORDER BY name")

    def list_vms(self, caller: Caller) -> list[dict]:
        if caller.is_admin:
            rows = self.db.all("SELECT * FROM vms ORDER BY created_at")
        else:
            rows = self.db.all("SELECT * FROM vms WHERE owner_id = ? ORDER BY created_at", (caller.id,))
        return [self._describe(r, with_secrets=False) for r in rows]

    def get_vm(self, caller: Caller, vm: str) -> dict:
        return self._describe(self._own(caller, vm))

    @journaled("create_vm", hide=())
    def create_vm(self, caller: Caller, image: str | None = None, from_vm: str | None = None,
                  from_snapshot: str | None = None, name: str | None = None,
                  vcpus: int | None = None, ram_mb: int | None = None,
                  permanent: bool = False, internet: bool = False) -> dict:
        if bool(image) == bool(from_vm and from_snapshot):
            raise StrataError("give either an image, or from_vm and from_snapshot",
                              "see list_images, or list_snapshots of a VM")
        if image:
            img = self.db.one("SELECT * FROM images WHERE name = ?", (image,))
            if img is None:
                raise NotFound(f"no image named {image!r}", "see list_images")
            backing = self.hv.volume_path(self.cfg.images_pool, img["volume"])
            os_, username, source = img["os"], img["username"], f"image {image}"
        else:
            parent = self._own(caller, from_vm)
            backing = self._snap(parent, from_snapshot)["disk"]
            os_, username, source = parent["os"], parent["username"], f"snapshot {from_vm}/{from_snapshot}"
        if permanent and not caller.is_admin:
            raise Forbidden("only the admin can create permanent VMs",
                            "create a disposable VM and ask the admin to keep it")

        vcpus = vcpus or OS_DEFAULTS[os_][0]
        ram_mb = ram_mb or OS_DEFAULTS[os_][1]
        for key, value in (("vcpus", vcpus), ("ram_mb", ram_mb)):
            lo, hi = LIMITS[key]
            if not lo <= value <= hi:
                raise StrataError(f"{key} must be between {lo} and {hi}")

        base = (image or from_vm)[:24].rstrip("-")
        name = _check_name("VM", name or f"{base}-{secrets.token_hex(2)}")

        with self._alloc_lock:
            if self.db.one("SELECT 1 FROM vms WHERE name = ?", (name,)):
                raise Conflict(f"a VM named {name!r} already exists", "pick another name")
            if not caller.is_admin:
                use = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(ram_mb), 0) ram, COALESCE(SUM(vcpus), 0) cpu"
                                  " FROM vms WHERE owner_id = ?", (caller.id,))
                if use["n"] + 1 > caller.max_vms:
                    raise Forbidden(f"quota reached: {use['n']}/{caller.max_vms} VMs", "destroy one first")
                if use["ram"] + ram_mb > caller.max_ram_mb:
                    raise Forbidden(f"quota reached: {use['ram'] + ram_mb} MB RAM requested, "
                                    f"limit {caller.max_ram_mb} MB", "use less RAM or destroy a VM")
                if use["cpu"] + vcpus > caller.max_vcpus:
                    raise Forbidden(f"quota reached: {use['cpu'] + vcpus} vCPUs requested, "
                                    f"limit {caller.max_vcpus}", "use fewer vCPUs or destroy a VM")
            ip, mac, ssh_port, rdp_port = self._allocate(os_)
            now = time.time()
            password = gen_password()
            self.db.execute(
                "INSERT INTO vms (name, owner_id, os, source, kind, vcpus, ram_mb, mac, ip, ssh_port,"
                " rdp_port, username, password, internet, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, caller.id, os_, source, "permanent" if permanent else "disposable", vcpus,
                 ram_mb, mac, ip, ssh_port, rdp_port, username, password, int(internet), now,
                 None if permanent else now + self.cfg.disposable_ttl_hours * 3600),
            )

        with self._vm_locks[name]:
            try:
                self.hv.add_dhcp_host(mac, ip, name)
                self._sync_network()
                self.hv.create_vm(VMSpec(name, os_, vcpus, ram_mb, mac, ip, backing))
                self.hv.wait_ready(name, READY_TIMEOUT[os_])
                self.hv.set_password(name, username, password)
            except Exception:
                self._remove(name, mac, ip)
                raise
        return self._describe(self.db.one("SELECT * FROM vms WHERE name = ?", (name,)))

    def _remove(self, name: str, mac: str, ip: str) -> None:
        for step in (lambda: self.hv.delete_vm(name), lambda: self.hv.remove_dhcp_host(mac, ip, name)):
            try:
                step()
            except Exception:
                log.exception("cleanup of %s", name)
        self.db.execute("DELETE FROM vms WHERE name = ?", (name,))
        self._sync_network()

    @journaled("destroy")
    def destroy(self, caller: Caller, vm: str) -> dict:
        row = self._own(caller, vm)
        if row["kind"] == "permanent" and not caller.is_admin:
            raise Forbidden(f"{vm!r} is a permanent VM, only the admin can destroy it")
        children = self.db.all("SELECT name, source FROM vms WHERE source LIKE ?", (f"snapshot {vm}/%",))
        if children:
            raise Conflict(f"{vm!r} has VMs forked from its snapshots: "
                           + ", ".join(c["name"] for c in children), "destroy those first")
        with self._vm_locks[vm]:
            self._remove(vm, row["mac"], row["ip"])
        return {"destroyed": vm}

    @journaled("power")
    def power(self, caller: Caller, vm: str, action: str) -> dict:
        row = self._own(caller, vm)
        actions = {"start": lambda: self.hv.start(vm),
                   "stop": lambda: self.hv.shutdown(vm),
                   "force_stop": lambda: self.hv.shutdown(vm, force=True),
                   "reboot": lambda: self.hv.reboot(vm)}
        if action not in actions:
            raise StrataError(f"unknown action {action!r}", "use one of: " + ", ".join(actions))
        with self._vm_locks[vm]:
            actions[action]()
            if action == "start":
                self.hv.wait_ready(vm, READY_TIMEOUT[row["os"]])
        return {"name": vm, "state": self.hv.state(vm)}

    @journaled("keep")
    def keep(self, caller: Caller, vm: str, hours: float | None = None, permanent: bool | None = None) -> dict:
        row = self._own(caller, vm)
        if permanent is not None:
            if not caller.is_admin:
                raise Forbidden("only the admin can make a VM permanent or disposable")
            self.db.execute("UPDATE vms SET kind = ?, expires_at = ? WHERE name = ?",
                            ("permanent" if permanent else "disposable",
                             None if permanent else time.time() + self.cfg.disposable_ttl_hours * 3600, vm))
        elif hours is not None:
            if row["kind"] == "permanent":
                raise Conflict(f"{vm!r} is permanent, it does not expire")
            if not caller.is_admin and hours > MAX_TTL_HOURS:
                raise Forbidden(f"at most {MAX_TTL_HOURS} hours", "ask the admin to make it permanent")
            self.db.execute("UPDATE vms SET expires_at = ? WHERE name = ?", (time.time() + hours * 3600, vm))
        return self._describe(self.db.one("SELECT * FROM vms WHERE name = ?", (vm,)), with_secrets=False)

    @journaled("set_internet")
    def set_internet(self, caller: Caller, vm: str, enabled: bool) -> dict:
        self._own(caller, vm)
        self.db.execute("UPDATE vms SET internet = ? WHERE name = ?", (int(enabled), vm))
        self._sync_network()
        return {"name": vm, "internet": enabled}

    # ---------- inside the guest ----------

    @journaled("exec", summarize=lambda r: {**r, "stdout": r["stdout"][:2000], "stderr": r["stderr"][:2000]})
    def exec(self, caller: Caller, vm: str, command: str, shell: str | None = None,
             timeout: float = 120) -> dict:
        row = self._own(caller, vm)
        self._running(row)
        shell = shell or DEFAULT_SHELL[row["os"]]
        if shell not in SHELLS[row["os"]]:
            raise StrataError(f"unknown shell {shell!r} for {row['os']}",
                              "use one of: " + ", ".join(SHELLS[row["os"]]))
        if not 1 <= timeout <= 3600:
            raise StrataError("timeout must be between 1 and 3600 seconds")
        start = time.monotonic()
        res = self.hv.exec(vm, SHELLS[row["os"]][shell](command), timeout)
        stdout, out_cut = _clip(res.stdout)
        stderr, err_cut = _clip(res.stderr)
        return {"exit_code": res.exit_code, "stdout": stdout, "stderr": stderr,
                "timed_out": res.timed_out, "truncated": out_cut or err_cut,
                "duration_s": round(time.monotonic() - start, 2)}

    @journaled("upload", hide=("data",))
    def upload(self, caller: Caller, vm: str, path: str, data: bytes) -> dict:
        row = self._own(caller, vm)
        self._running(row)
        self.hv.write_file(vm, path, data)
        return {"path": path, "bytes": len(data)}

    @journaled("download", summarize=lambda r: {"bytes": len(r)})
    def download(self, caller: Caller, vm: str, path: str) -> bytes:
        row = self._own(caller, vm)
        self._running(row)
        return self.hv.read_file(vm, path, MAX_DOWNLOAD)

    # ---------- screen, keyboard, mouse ----------

    @journaled("screenshot", summarize=lambda r: {"bytes": len(r)})
    def screenshot(self, caller: Caller, vm: str) -> bytes:
        self._running(self._own(caller, vm))
        return self.hv.screenshot(vm)

    @journaled("click")
    def click(self, caller: Caller, vm: str, x: int, y: int, button: str = "left",
              double: bool = False) -> dict:
        self._running(self._own(caller, vm))
        if button not in ("left", "right", "middle"):
            raise StrataError("button must be left, right or middle")
        width, height = self.hv.screen_size(vm)
        if not (0 <= x < width and 0 <= y < height):
            raise StrataError(f"({x}, {y}) is outside the screen ({width}x{height})",
                              "take a screenshot to see the coordinates")
        self.hv.click(vm, x, y, button, double)
        return {"clicked": [x, y]}

    @journaled("type_text")
    def type_text(self, caller: Caller, vm: str, text: str) -> dict:
        self._running(self._own(caller, vm))
        self.hv.type_text(vm, text)
        return {"typed": len(text)}

    @journaled("send_keys")
    def send_keys(self, caller: Caller, vm: str, keys: list[str]) -> dict:
        self._running(self._own(caller, vm))
        self.hv.send_keys(vm, keys)
        return {"pressed": keys}

    # ---------- states ----------

    def list_snapshots(self, caller: Caller, vm: str) -> list[dict]:
        self._own(caller, vm)
        return self.db.all("SELECT name, with_memory, created_at FROM snapshots WHERE vm = ?"
                           " ORDER BY created_at", (vm,))

    @journaled("snapshot")
    def snapshot(self, caller: Caller, vm: str, name: str, with_memory: bool = True) -> dict:
        row = self._own(caller, vm)
        _check_name("snapshot", name)
        if self.db.one("SELECT 1 FROM snapshots WHERE vm = ? AND name = ?", (vm, name)):
            raise Conflict(f"{vm!r} already has a snapshot named {name!r}", "pick another name")
        with self._vm_locks[vm]:
            memory = with_memory and self.hv.state(vm) == "running"
            disk = self.hv.snapshot(vm, name, memory)
            self.db.execute("INSERT INTO snapshots (vm, name, with_memory, disk, created_at)"
                            " VALUES (?, ?, ?, ?, ?)", (vm, name, int(memory), disk, time.time()))
        return {"vm": vm, "snapshot": name, "with_memory": memory}

    @journaled("revert")
    def revert(self, caller: Caller, vm: str, snapshot: str) -> dict:
        row = self._own(caller, vm)
        snap = self._snap(row, snapshot)
        with self._vm_locks[vm]:
            self.hv.revert(vm, snapshot, snap["disk"])
            if snap["with_memory"]:
                self.hv.wait_ready(vm, 120)
        return {"vm": vm, "reverted_to": snapshot, "state": self.hv.state(vm)}

    @journaled("delete_snapshot")
    def delete_snapshot(self, caller: Caller, vm: str, snapshot: str) -> dict:
        row = self._own(caller, vm)
        self._snap(row, snapshot)
        children = self.db.all("SELECT name FROM vms WHERE source = ?", (f"snapshot {vm}/{snapshot}",))
        if children:
            raise Conflict(f"VMs were forked from {snapshot!r}: " + ", ".join(c["name"] for c in children),
                           "destroy those first")
        with self._vm_locks[vm]:
            self.hv.delete_snapshot(vm, snapshot)
            self.db.execute("DELETE FROM snapshots WHERE vm = ? AND name = ?", (vm, snapshot))
        return {"vm": vm, "deleted": snapshot}

    def fork(self, caller: Caller, vm: str, snapshot: str, name: str | None = None,
             internet: bool = False) -> dict:
        return self.create_vm(caller, from_vm=vm, from_snapshot=snapshot, name=name, internet=internet)

    @journaled("diff", summarize=lambda r: {"changes": r["total"]})
    def diff(self, caller: Caller, vm: str, snapshot_a: str, snapshot_b: str,
             limit: int = 2000) -> dict:
        """What changed on disk between two snapshots of a VM (added, deleted, changed files)."""
        row = self._own(caller, vm)
        disk_a = self._snap(row, snapshot_a)["disk"]
        disk_b = self._snap(row, snapshot_b)["disk"]
        changes = self.hv.diff_disks(disk_a, disk_b)
        total = len(changes)
        counts = {k: sum(1 for c in changes if c["change"] == k) for k in ("added", "deleted", "changed")}
        return {"vm": vm, "from": snapshot_a, "to": snapshot_b, "total": total,
                "counts": counts, "truncated": total > limit, "changes": changes[:limit]}

    # ---------- journal and housekeeping ----------

    def history(self, caller: Caller, vm: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        if vm:
            self._own(caller, vm)
            rows = self.db.all("SELECT * FROM journal WHERE vm = ? ORDER BY id DESC LIMIT ?", (vm, limit))
        elif caller.is_admin:
            rows = self.db.all("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.db.all("SELECT * FROM journal WHERE actor = ? ORDER BY id DESC LIMIT ?",
                               (caller.name, limit))
        return rows

    def reap_expired(self) -> list[str]:
        """Destroy disposable VMs past their expiry. Called periodically by the server."""
        reaped = []
        for row in self.db.all("SELECT * FROM vms WHERE kind = 'disposable' AND expires_at < ?",
                               (time.time(),)):
            if self.db.one("SELECT 1 FROM vms WHERE source LIKE ?", (f"snapshot {row['name']}/%",)):
                continue  # forks depend on it; it goes once they are gone
            with self._vm_locks[row["name"]]:
                self._remove(row["name"], row["mac"], row["ip"])
            self.db.log("strata", row["name"], "expired", {}, True, {"destroyed": row["name"]}, 0)
            reaped.append(row["name"])
        return reaped

    def resync(self) -> None:
        """Re-apply network state after a restart (the firewall forgets dynamic entries on reload)."""
        self._sync_network()

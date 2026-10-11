"""Hypervisor implementation on libvirt/QEMU.

Disk layout, per VM, in the VMs pool:
    NAME.0.qcow2, NAME.1.qcow2, ...   overlays; the highest is the active disk
    NAME.SNAP.mem                      memory state of a snapshot taken with memory
A snapshot freezes the active overlay and continues on a new one on top of it, so the
frozen file is exactly the disk at that moment. Reverting creates a fresh overlay on top
of the frozen file (and restores memory when there is some). Forks do the same in a new VM.

The virtual hardware is deliberately minimal: virtio disk/net/serial/input/rng, a virtio
display without 3D, no USB controller, no sound, no shared folders, no clipboard channel,
and a VNC display with no listening socket at all.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

import libvirt
import libvirt_qemu
from PIL import Image

from .config import Config
from .errors import Conflict, NotFound, StrataError
from .hypervisor import ExecResult, VMSpec

log = logging.getLogger(__name__)

PREFIX = "strata-"
NS = "https://github.com/Vasco0x4/Strata"
CHUNK = 1024 * 1024

# Friendly key names -> QEMU qcodes
KEY_ALIASES = {
    "enter": "ret", "return": "ret", "esc": "esc", "escape": "esc", "space": "spc",
    "win": "meta_l", "windows": "meta_l", "super": "meta_l", "cmd": "meta_l",
    "del": "delete", "pageup": "pgup", "pagedown": "pgdn", "ins": "insert",
    "control": "ctrl", "backspace": "backspace", "tab": "tab",
}
# US keyboard layout: character -> (qcode, shift)
US_KEYS = {**{c: (c, False) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
           **{c.upper(): (c, True) for c in "abcdefghijklmnopqrstuvwxyz"},
           " ": ("spc", False), "\n": ("ret", False), "\t": ("tab", False),
           "-": ("minus", False), "_": ("minus", True), "=": ("equal", False), "+": ("equal", True),
           "[": ("bracket_left", False), "{": ("bracket_left", True),
           "]": ("bracket_right", False), "}": ("bracket_right", True),
           ";": ("semicolon", False), ":": ("semicolon", True),
           "'": ("apostrophe", False), '"': ("apostrophe", True),
           "`": ("grave_accent", False), "~": ("grave_accent", True),
           "\\": ("backslash", False), "|": ("backslash", True),
           ",": ("comma", False), "<": ("comma", True), ".": ("dot", False), ">": ("dot", True),
           "/": ("slash", False), "?": ("slash", True),
           "!": ("1", True), "@": ("2", True), "#": ("3", True), "$": ("4", True), "%": ("5", True),
           "^": ("6", True), "&": ("7", True), "*": ("8", True), "(": ("9", True), ")": ("0", True)}


def _domain_xml(cfg: Config, spec: VMSpec, disk: str) -> str:
    windows = spec.os == "windows"
    hyperv = """
    <hyperv mode='custom'>
      <relaxed state='on'/><vapic state='on'/><spinlocks state='on' retries='8191'/>
      <vpindex state='on'/><runtime state='on'/><synic state='on'/><stimer state='on'/>
      <frequencies state='on'/><tlbflush state='on'/><ipi state='on'/>
    </hyperv>""" if windows else ""
    # Firmware auto-selection constrained to a Secure Boot build with Microsoft keys already
    # enrolled (Windows 11 requires this). SMM (in <features> below) protects the var store.
    # Per libvirt docs this feature block is the mechanism; a separate <loader secure='yes'/>
    # is redundant with it, so it is intentionally not included.
    secure_boot = """
    <firmware>
      <feature enabled='yes' name='secure-boot'/>
      <feature enabled='yes' name='enrolled-keys'/>
    </firmware>""" if windows else ""
    tpm = """
    <tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>""" if windows else ""
    return f"""<domain type='kvm'>
  <name>{PREFIX}{spec.name}</name>
  <metadata><strata:vm xmlns:strata='{NS}' name={quoteattr(spec.name)} os={quoteattr(spec.os)}/></metadata>
  <memory unit='MiB'>{spec.ram_mb}</memory>
  <vcpu>{spec.vcpus}</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>{secure_boot}
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/><smm state='on'/>{hyperv}</features>
  <cpu mode='host-passthrough' check='none'/>
  <clock offset='{"localtime" if windows else "utc"}'>
    <timer name='rtc' tickpolicy='catchup'/><timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>{"<timer name='hypervclock' present='yes'/>" if windows else ""}
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <pm><suspend-to-mem enabled='no'/><suspend-to-disk enabled='no'/></pm>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file={quoteattr(disk)}/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <controller type='usb' model='none'/>
    <interface type='network'>
      <source network={quoteattr(cfg.network)}/>
      <mac address='{spec.mac}'/>
      <model type='virtio'/>
      <filterref filter='clean-traffic'>
        <parameter name='CTRL_IP_LEARNING' value='dhcp'/>
      </filterref>
    </interface>
    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>
    <input type='tablet' bus='virtio'/>
    <input type='keyboard' bus='virtio'/>
    <graphics type='vnc'><listen type='none'/></graphics>
    <video><model type='virtio' heads='1'/></video>{tpm}
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
    <memballoon model='none'/>
  </devices>
</domain>"""


class LibvirtHypervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._conn: libvirt.virConnect | None = None
        self._lock = threading.Lock()
        self._screen: dict[str, tuple[int, int]] = {}

    # ---------- plumbing ----------

    @property
    def conn(self) -> libvirt.virConnect:
        with self._lock:
            if self._conn is None or not self._conn.isAlive():
                self._conn = libvirt.open(self.cfg.libvirt_uri)
            return self._conn

    def _dom(self, name: str) -> libvirt.virDomain:
        try:
            return self.conn.lookupByName(PREFIX + name)
        except libvirt.libvirtError:
            raise NotFound(f"VM {name!r} does not exist in the hypervisor") from None

    def _pool(self) -> libvirt.virStoragePool:
        return self.conn.storagePoolLookupByName(self.cfg.vms_pool)

    def _pool_dir(self) -> str:
        return ET.fromstring(self._pool().XMLDesc(0)).findtext("target/path")

    def _active_disk(self, dom: libvirt.virDomain) -> str:
        xml = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        return xml.find("devices/disk[@device='disk']/source").get("file")

    def _new_overlay(self, name: str, backing: str) -> str:
        """Create the next NAME.N.qcow2 on top of backing; return its path."""
        pool = self._pool()
        pool.refresh(0)
        used = [v for v in pool.listVolumes() if v.startswith(name + ".") and v.endswith(".qcow2")]
        n = 1 + max((int(v.split(".")[-2]) for v in used if v.split(".")[-2].isdigit()), default=-1)
        capacity = self.conn.storageVolLookupByPath(backing).info()[1]
        vol = pool.createXML(f"""<volume>
  <name>{name}.{n}.qcow2</name>
  <capacity unit='bytes'>{capacity}</capacity>
  <target><format type='qcow2'/><permissions><mode>0660</mode></permissions></target>
  <backingStore><path>{backing}</path><format type='qcow2'/></backingStore>
</volume>""", 0)
        return vol.path()

    def _set_disk(self, dom: libvirt.virDomain, path: str) -> str:
        xml = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        disk = xml.find("devices/disk[@device='disk']")
        disk.find("source").set("file", path)
        # Drop the stale <backingStore>: it describes the previous disk's chain, and libvirt
        # (and its AppArmor helper) would trust that truncated chain instead of re-probing the
        # new qcow2 — leaving the real backing files, e.g. the base image, out of the profile,
        # so qemu is denied access and the VM fails to start. Removing it makes libvirt re-detect
        # the full chain from the file, exactly as it does for a freshly created VM.
        for bs in disk.findall("backingStore"):
            disk.remove(bs)
        new = ET.tostring(xml, encoding="unicode")
        self.conn.defineXML(new)
        return new

    def _agent(self, name: str, command: str, args: dict | None = None, timeout: int = 30):
        req = {"execute": command}
        if args is not None:
            req["arguments"] = args
        try:
            out = libvirt_qemu.qemuAgentCommand(self._dom(name), json.dumps(req), timeout, 0)
        except libvirt.libvirtError as e:
            raise StrataError(f"guest agent: {e.get_error_message()}",
                              "the VM may still be booting; try again shortly") from None
        return json.loads(out).get("return")

    def _monitor(self, name: str, command: str, args: dict) -> None:
        libvirt_qemu.qemuMonitorCommand(self._dom(name), json.dumps({"execute": command, "arguments": args}), 0)

    # ---------- lifecycle ----------

    def volume_path(self, pool: str, volume: str) -> str:
        try:
            return self.conn.storagePoolLookupByName(pool).storageVolLookupByName(volume).path()
        except libvirt.libvirtError:
            raise NotFound(f"volume {volume!r} not found in pool {pool!r}") from None

    def create_vm(self, spec: VMSpec) -> None:
        disk = self._new_overlay(spec.name, spec.backing)
        dom = self.conn.defineXML(_domain_xml(self.cfg, spec, disk))
        dom.create()

    def delete_vm(self, name: str) -> None:
        try:
            dom = self.conn.lookupByName(PREFIX + name)
        except libvirt.libvirtError:
            dom = None
        if dom is not None:
            if dom.isActive():
                dom.destroy()
            dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_TPM
                              | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                              | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
        pool = self._pool()
        pool.refresh(0)
        # Delete overlays top-down so no file is removed while another still points at it
        vols = sorted((v for v in pool.listVolumes() if v.startswith(name + ".")), reverse=True)
        for v in vols:
            pool.storageVolLookupByName(v).delete(0)
        self._screen.pop(name, None)

    def state(self, name: str) -> str:
        try:
            st = self.conn.lookupByName(PREFIX + name).state()[0]
        except libvirt.libvirtError:
            return "missing"
        return {libvirt.VIR_DOMAIN_RUNNING: "running", libvirt.VIR_DOMAIN_PAUSED: "paused",
                libvirt.VIR_DOMAIN_SHUTOFF: "shutoff", libvirt.VIR_DOMAIN_CRASHED: "shutoff",
                libvirt.VIR_DOMAIN_SHUTDOWN: "running"}.get(st, "unknown")

    def start(self, name: str) -> None:
        dom = self._dom(name)
        if not dom.isActive():
            dom.create()

    def shutdown(self, name: str, force: bool = False) -> None:
        dom = self._dom(name)
        if not dom.isActive():
            return
        if force:
            dom.destroy()
            return
        dom.shutdownFlags(libvirt.VIR_DOMAIN_SHUTDOWN_AGENT | libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)
        deadline = time.monotonic() + 180
        while dom.isActive():
            if time.monotonic() > deadline:
                raise StrataError("the VM did not shut down within 3 minutes",
                                  "use power(action='force_stop')")
            time.sleep(2)

    def reboot(self, name: str) -> None:
        self._dom(name).reboot(libvirt.VIR_DOMAIN_REBOOT_AGENT | libvirt.VIR_DOMAIN_REBOOT_ACPI_POWER_BTN)

    def wait_ready(self, name: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if not self._dom(name).isActive():
                raise Conflict(f"VM {name!r} stopped while booting")
            try:
                libvirt_qemu.qemuAgentCommand(self._dom(name), '{"execute":"guest-ping"}', 5, 0)
                return
            except libvirt.libvirtError:
                pass
            if time.monotonic() > deadline:
                raise StrataError(f"VM {name!r} did not become ready within {int(timeout)} s",
                                  "look at its screen with screenshot")
            time.sleep(3)

    # ---------- inside the guest ----------

    def exec(self, name: str, argv: list[str], timeout: float, stdin: bytes | None = None) -> ExecResult:
        args = {"path": argv[0], "arg": argv[1:], "capture-output": True}
        if stdin is not None:
            args["input-data"] = base64.b64encode(stdin).decode()
        pid = self._agent(name, "guest-exec", args)["pid"]
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            st = self._agent(name, "guest-exec-status", {"pid": pid})
            if st.get("exited"):
                return ExecResult(st.get("exitcode", st.get("signal")),
                                  base64.b64decode(st.get("out-data", "")),
                                  base64.b64decode(st.get("err-data", "")))
            if time.monotonic() > deadline:
                return ExecResult(None, b"", b"", timed_out=True)
            time.sleep(delay)
            delay = min(delay * 2, 1.0)

    def write_file(self, name: str, path: str, data: bytes) -> None:
        handle = self._agent(name, "guest-file-open", {"path": path, "mode": "wb"})
        try:
            for i in range(0, len(data), CHUNK):
                self._agent(name, "guest-file-write",
                            {"handle": handle, "buf-b64": base64.b64encode(data[i:i + CHUNK]).decode()}, 120)
        finally:
            self._agent(name, "guest-file-close", {"handle": handle})

    def read_file(self, name: str, path: str, max_bytes: int) -> bytes:
        handle = self._agent(name, "guest-file-open", {"path": path, "mode": "rb"})
        out = bytearray()
        try:
            while True:
                r = self._agent(name, "guest-file-read", {"handle": handle, "count": CHUNK}, 120)
                out += base64.b64decode(r.get("buf-b64", ""))
                if len(out) > max_bytes:
                    raise StrataError(f"file is larger than {max_bytes // (1024 * 1024)} MB")
                if r.get("eof") or not r.get("count"):
                    return bytes(out)
        finally:
            self._agent(name, "guest-file-close", {"handle": handle})

    def set_password(self, name: str, user: str, password: str) -> None:
        self._agent(name, "guest-set-user-password",
                    {"username": user, "password": base64.b64encode(password.encode()).decode(),
                     "crypted": False})

    # ---------- screen, keyboard, mouse ----------

    def screenshot(self, name: str) -> bytes:
        dom = self._dom(name)
        stream = self.conn.newStream(0)
        try:
            dom.screenshot(stream, 0, 0)
            raw = bytearray()
            stream.recvAll(lambda _s, data, buf: buf.extend(data), raw)
            stream.finish()
        except libvirt.libvirtError:
            stream.abort()
            raise
        img = Image.open(io.BytesIO(raw))
        self._screen[name] = img.size
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()

    def screen_size(self, name: str) -> tuple[int, int]:
        if name not in self._screen:
            self.screenshot(name)
        return self._screen[name]

    def send_keys(self, name: str, keys: list[str]) -> None:
        codes = [{"type": "qcode", "data": KEY_ALIASES.get(k.lower(), k.lower())} for k in keys]
        try:
            self._monitor(name, "send-key", {"keys": codes, "hold-time": 80})
        except libvirt.libvirtError as e:
            raise StrataError(f"cannot press {keys}: {e.get_error_message()}",
                              "use names like ctrl, alt, shift, win, enter, esc, tab, f1, a, 1") from None

    def type_text(self, name: str, text: str) -> None:
        missing = sorted({c for c in text if c not in US_KEYS})
        if missing:
            raise StrataError(f"cannot type {''.join(missing)!r} with a US keyboard",
                              "use exec or upload for non-ASCII text")
        for c in text:
            code, shift = US_KEYS[c]
            keys = ([{"type": "qcode", "data": "shift"}] if shift else []) + [{"type": "qcode", "data": code}]
            self._monitor(name, "send-key", {"keys": keys, "hold-time": 20})
            time.sleep(0.02)

    def click(self, name: str, x: int, y: int, button: str = "left", double: bool = False) -> None:
        width, height = self.screen_size(name)
        ax = round(x * 32767 / max(width - 1, 1))
        ay = round(y * 32767 / max(height - 1, 1))
        self._monitor(name, "input-send-event", {"events": [
            {"type": "abs", "data": {"axis": "x", "value": ax}},
            {"type": "abs", "data": {"axis": "y", "value": ay}}]})
        for _ in range(2 if double else 1):
            for down in (True, False):
                self._monitor(name, "input-send-event",
                              {"events": [{"type": "btn", "data": {"down": down, "button": button}}]})
                time.sleep(0.05)

    def open_console(self, name: str) -> int:
        return self._dom(name).openGraphicsFD(0, libvirt.VIR_DOMAIN_OPEN_GRAPHICS_SKIPAUTH)

    # ---------- states ----------

    def snapshot(self, name: str, snap: str, with_memory: bool) -> str:
        dom = self._dom(name)
        frozen = self._active_disk(dom)
        if not dom.isActive():
            self._set_disk(dom, self._new_overlay(name, frozen))
            return frozen

        # Pre-create the new active overlay ourselves (mode 0644) and have libvirt reuse it,
        # instead of letting snapshotCreateXML create it at 0600 owned by libvirt-qemu. This
        # way, once this overlay is frozen by a later snapshot, libvirt restores our 0644 and
        # the unprivileged strata service can still read it for an offline diff.
        new_disk = self._new_overlay(name, frozen)
        pool = self._pool()
        memory = (f"<memory snapshot='external' file={quoteattr(f'{self._pool_dir()}/{name}.{snap}.mem')}/>"
                  if with_memory else "<memory snapshot='no'/>")
        xml = f"""<domainsnapshot>
  <name>{snap}</name>
  {memory}
  <disks><disk name='vda' snapshot='external'><driver type='qcow2'/><source file={quoteattr(new_disk)}/></disk></disks>
</domainsnapshot>"""
        flags = (libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
                 | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT)
        if with_memory:
            dom.snapshotCreateXML(xml, flags | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_LIVE)
        else:
            flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
            try:
                dom.snapshotCreateXML(xml, flags | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE)
            except libvirt.libvirtError:
                dom.snapshotCreateXML(xml, flags)  # guest could not freeze its filesystems
        # Keep the persistent definition on the new active disk too
        self._set_disk(dom, new_disk)
        pool.refresh(0)
        return frozen

    def revert(self, name: str, snap: str, disk: str) -> None:
        dom = self._dom(name)
        abandoned = self._active_disk(dom)  # never a frozen state: snapshots always move to a new overlay
        if dom.isActive():
            dom.destroy()
        new_disk = self._new_overlay(name, disk)
        self._set_disk(dom, new_disk)
        pool = self._pool()
        pool.refresh(0)
        if f"{name}.{snap}.mem" in pool.listVolumes():
            mem = pool.storageVolLookupByName(f"{name}.{snap}.mem").path()
            saved = ET.fromstring(self.conn.saveImageGetXMLDesc(mem, libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE))
            saved.find("devices/disk[@device='disk']/source").set("file", new_disk)
            self.conn.restoreFlags(mem, ET.tostring(saved, encoding="unicode"), libvirt.VIR_DOMAIN_SAVE_RUNNING)
        else:
            dom.create()
        if abandoned != disk:
            try:
                self.conn.storageVolLookupByPath(abandoned).delete(0)
            except libvirt.libvirtError:
                log.warning("could not delete abandoned overlay %s", abandoned)

    def delete_snapshot(self, name: str, snap: str) -> None:
        # The frozen disk may still back later states or forks; only the memory state goes.
        pool = self._pool()
        pool.refresh(0)
        if f"{name}.{snap}.mem" in pool.listVolumes():
            pool.storageVolLookupByName(f"{name}.{snap}.mem").delete(0)

    def diff_disks(self, disk_a: str, disk_b: str) -> list[dict]:
        # virt-diff walks both disks read-only and reports what differs. It needs the
        # whole qcow2 backing chain, which resolves automatically from the overlay paths.
        cmd = ["virt-diff", "--atime", "-a", disk_a, "-A", disk_b]
        # A prebuilt fixed appliance (made as root at install time) means virt-diff does not
        # need to read the host kernel in /boot, which is root-only; the service stays unprivileged.
        env = {"LIBGUESTFS_BACKEND": "direct", "PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp"}
        if self.cfg.appliance_dir.is_dir():
            env["LIBGUESTFS_PATH"] = str(self.cfg.appliance_dir)
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=1800, env=env)
        except FileNotFoundError:
            raise StrataError("virt-diff is not installed", "install libguestfs-tools") from None
        except subprocess.TimeoutExpired:
            raise StrataError("comparison timed out", "the disks are very large") from None
        if out.returncode != 0:
            raise StrataError("comparison failed: " + out.stderr.decode(errors="replace")[:400])
        return self._parse_virt_diff(out.stdout.decode(errors="replace"))

    @staticmethod
    def _parse_virt_diff(text: str) -> list[dict]:
        # Lines look like: "+ - 0644  1234  /path"  (added), "- ..." (deleted), "= ..." (changed).
        marks = {"+": "added", "-": "deleted", "=": "changed"}
        changes: list[dict] = []
        for line in text.splitlines():
            if len(line) < 2 or line[0] not in marks or line[1] != " ":
                continue
            rest = line[2:].strip()
            parts = rest.split(None, 3)
            path = parts[-1] if parts else rest
            details = " ".join(parts[:-1]) if len(parts) > 1 else ""
            changes.append({"change": marks[line[0]], "path": path, "details": details})
        return changes

    # ---------- network ----------

    def get_ip(self, name: str, subnet_prefix: str) -> str | None:
        """The VM's current IPv4 on the lab subnet, read from the guest agent.

        We do not pin IPs: dnsmasq assigns a sticky per-MAC lease, so this is stable across
        reboots and reverts. The clean-traffic filter learns the same address from DHCP and
        blocks any other source, so the VM cannot spoof it.
        """
        try:
            ifaces = self._dom(name).interfaceAddresses(
                libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT, 0)
        except libvirt.libvirtError:
            return None
        for iface in (ifaces or {}).values():
            for addr in iface.get("addrs") or []:
                ip = addr.get("addr", "")
                if addr.get("type") == libvirt.VIR_IP_ADDR_TYPE_IPV4 and ip.startswith(subnet_prefix + "."):
                    return ip
        return None

"""What the service needs from the hypervisor. The real implementation is in libvirt_hv.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VMSpec:
    name: str
    os: str            # "windows" or "linux"
    vcpus: int
    ram_mb: int
    mac: str
    ip: str
    backing: str       # path of the read-only disk this VM starts from


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None    # None when the command timed out
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


class Hypervisor(Protocol):
    def volume_path(self, pool: str, volume: str) -> str: ...

    # Lifecycle
    def create_vm(self, spec: VMSpec) -> None:
        """Create the VM's thin disk on top of spec.backing, define it with minimal hardware, start it."""
    def delete_vm(self, name: str) -> None:
        """Stop the VM and remove it with all its disks, memory states and firmware files."""
    def state(self, name: str) -> str:
        """One of: running, paused, shutoff, missing."""
    def start(self, name: str) -> None: ...
    def shutdown(self, name: str, force: bool = False) -> None: ...
    def reboot(self, name: str) -> None: ...
    def wait_ready(self, name: str, timeout: float) -> None:
        """Wait until the guest agent answers."""

    # Inside the guest (through the guest agent)
    def exec(self, name: str, argv: list[str], timeout: float, stdin: bytes | None = None) -> ExecResult: ...
    def write_file(self, name: str, path: str, data: bytes) -> None: ...
    def read_file(self, name: str, path: str, max_bytes: int) -> bytes: ...
    def set_password(self, name: str, user: str, password: str) -> None: ...

    # Screen, keyboard, mouse
    def screenshot(self, name: str) -> bytes:
        """PNG of the VM's screen."""
    def screen_size(self, name: str) -> tuple[int, int]: ...
    def send_keys(self, name: str, keys: list[str]) -> None:
        """Press keys together, then release, e.g. ["ctrl", "alt", "delete"]."""
    def type_text(self, name: str, text: str) -> None: ...
    def click(self, name: str, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def open_console(self, name: str) -> int:
        """File descriptor of a connected socket speaking VNC to the VM's display."""

    # States
    def snapshot(self, name: str, snap: str, with_memory: bool) -> str:
        """Freeze the current state; return the path of the frozen disk."""
    def revert(self, name: str, snap: str, disk: str) -> None:
        """Go back to the state frozen in disk (and its memory, if the snapshot has some)."""
    def delete_snapshot(self, name: str, snap: str) -> None: ...

    # Network
    def add_dhcp_host(self, mac: str, ip: str, name: str) -> None: ...
    def remove_dhcp_host(self, mac: str, ip: str, name: str) -> None: ...


class NetworkControl(Protocol):
    def sync(self, forwards: list[tuple[int, str, int]], internet: list[str]) -> None:
        """Replace the firewall's port forwards and internet allow-list with exactly these."""

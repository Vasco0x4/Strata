"""Settings, read from /etc/strata/strata.toml (or $STRATA_CONFIG)."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _default_public_host() -> str:
    """Address of this host on the LAN, as seen by clients."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("1.1.1.1", 53))  # no packet is sent for UDP connect
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


@dataclass
class Quota:
    max_vms: int = 3
    max_ram_mb: int = 16384
    max_vcpus: int = 8


@dataclass
class Config:
    listen: str = "0.0.0.0"
    port: int = 8080
    public_host: str = field(default_factory=_default_public_host)
    libvirt_uri: str = "qemu:///system"
    data_dir: Path = Path("/var/lib/strata")
    images_pool: str = "strata-images"
    vms_pool: str = "strata-vms"
    network: str = "strata"
    subnet_prefix: str = "10.99.0"
    forward_ports: tuple[int, int] = (30000, 30999)
    netctl_socket: Path = Path("/run/strata-netctl.sock")
    admin_socket: Path = Path("/run/strata/admin.sock")
    disposable_ttl_hours: int = 24
    default_quota: Quota = field(default_factory=Quota)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "strata.db"

    @property
    def base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}"


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("STRATA_CONFIG", "/etc/strata/strata.toml"))
    cfg = Config()
    if not path.exists():
        return cfg
    raw = tomllib.loads(path.read_text())
    quota = raw.pop("quota", {})
    for key, value in raw.items():
        if not hasattr(cfg, key):
            raise ValueError(f"{path}: unknown setting {key!r}")
        current = getattr(cfg, key)
        if isinstance(current, Path):
            value = Path(value)
        elif isinstance(current, tuple):
            value = tuple(value)
        setattr(cfg, key, value)
    for key, value in quota.items():
        if not hasattr(cfg.default_quota, key):
            raise ValueError(f"{path}: unknown quota setting {key!r}")
        setattr(cfg.default_quota, key, value)
    return cfg

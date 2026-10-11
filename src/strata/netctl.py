"""Client for the root helper that applies port forwards and internet access (helpers/strata-netctl)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .errors import StrataError


class NetctlClient:
    def __init__(self, path: Path, timeout: float = 15):
        self.path, self.timeout = str(path), timeout

    def sync(self, forwards: list[tuple[int, str, int]], internet: list[str]) -> None:
        req = json.dumps({"forwards": [list(f) for f in forwards], "internet": list(internet)}).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(self.path)
            s.sendall(req)
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := s.recv(4096):
                reply += chunk
        res = json.loads(reply or b"{}")
        if not res.get("ok"):
            raise StrataError(f"firewall update refused: {res.get('error', 'no reply')}")

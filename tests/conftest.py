from __future__ import annotations

import pytest

from strata import auth
from strata.config import Config, Quota
from strata.db import Database
from strata.hypervisor import ExecResult, VMSpec
from strata.service import Service


class FakeHypervisor:
    def __init__(self):
        self.vms: dict[str, dict] = {}
        self.dhcp: set[tuple[str, str, str]] = set()
        self.fail_create = False

    def volume_path(self, pool, volume):
        return f"/srv/strata/{pool}/{volume}"

    def create_vm(self, spec: VMSpec):
        if self.fail_create:
            raise RuntimeError("boom")
        self.vms[spec.name] = {"spec": spec, "state": "running", "snaps": {}, "files": {}, "password": None}

    def delete_vm(self, name):
        self.vms.pop(name, None)

    def state(self, name):
        return self.vms[name]["state"] if name in self.vms else "missing"

    def start(self, name):
        self.vms[name]["state"] = "running"

    def shutdown(self, name, force=False):
        self.vms[name]["state"] = "shutoff"

    def reboot(self, name):
        pass

    def wait_ready(self, name, timeout):
        pass

    def exec(self, name, argv, timeout, stdin=None):
        return ExecResult(0, (" ".join(argv)).encode(), b"")

    def write_file(self, name, path, data):
        self.vms[name]["files"][path] = data

    def read_file(self, name, path, max_bytes):
        return self.vms[name]["files"][path][:max_bytes]

    def set_password(self, name, user, password):
        self.vms[name]["password"] = (user, password)

    def screenshot(self, name):
        return b"\x89PNG fake"

    def screen_size(self, name):
        return (1280, 800)

    def send_keys(self, name, keys):
        pass

    def type_text(self, name, text):
        pass

    def click(self, name, x, y, button="left", double=False):
        pass

    def snapshot(self, name, snap, with_memory):
        path = f"/srv/strata/strata-vms/{name}.{snap}.qcow2"
        self.vms[name]["snaps"][snap] = path
        return path

    def revert(self, name, snap, disk):
        assert self.vms[name]["snaps"][snap] == disk
        self.vms[name]["reverted"] = snap

    def delete_snapshot(self, name, snap):
        del self.vms[name]["snaps"][snap]

    def add_dhcp_host(self, mac, ip, name):
        self.dhcp.add((mac, ip, name))

    def remove_dhcp_host(self, mac, ip, name):
        self.dhcp.discard((mac, ip, name))


class FakeNet:
    def __init__(self):
        self.forwards: list = []
        self.internet: list = []

    def sync(self, forwards, internet):
        self.forwards, self.internet = sorted(forwards), sorted(internet)


@pytest.fixture
def env():
    cfg = Config(public_host="10.0.0.5")
    db = Database(":memory:")
    db.execute("INSERT INTO images (name, os, volume, username, created_at)"
               " VALUES ('win11', 'windows', 'win11.qcow2', 'strata', 0),"
               "        ('ubuntu', 'linux', 'ubuntu.qcow2', 'strata', 0)")
    hv, net = FakeHypervisor(), FakeNet()
    svc = Service(cfg, db, hv, net)

    def caller(name, admin=False, quota=Quota()):
        token = auth.create_agent(db, name, quota, is_admin=admin)
        return auth.authenticate(db, token)

    return svc, hv, net, caller

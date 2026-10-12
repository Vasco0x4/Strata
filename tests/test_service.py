import pytest

from strata.config import Quota
from strata.errors import Conflict, Forbidden, NotFound, StrataError


def test_create_vm_from_image(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    vm = svc.create_vm(alice, image="win11", name="w1")
    assert vm["name"] == "w1" and vm["os"] == "windows" and vm["state"] == "running"
    assert vm["kind"] == "disposable" and vm["expires_in_hours"] == pytest.approx(24, abs=0.1)
    spec = hv.vms["w1"]["spec"]
    assert spec.backing == "/srv/strata/strata-images/win11.qcow2"
    assert (spec.vcpus, spec.ram_mb) == (4, 8192)
    # the VM gets its own password, set through the guest agent
    assert hv.vms["w1"]["password"] == ("strata", vm["access"]["password"])
    # SSH and RDP forwarded to the VM's address
    ip = spec.ip
    ssh, rdp = vm["access"]["ssh"], vm["access"]["rdp"]
    assert ssh.startswith("ssh -p 30000 strata@10.0.0.5") and rdp == "10.0.0.5:30001"
    assert net.forwards == [(30000, ip, 22), (30001, ip, 3389)]
    assert net.internet == []


def test_linux_vm_has_no_rdp(env):
    svc, hv, net, caller = env
    vm = svc.create_vm(caller("alice"), image="ubuntu")
    assert vm["access"]["rdp"] is None
    assert len(net.forwards) == 1


def test_agents_only_see_their_vms(env):
    svc, hv, net, caller = env
    alice, bob, admin = caller("alice"), caller("bob"), caller("root", admin=True)
    svc.create_vm(alice, image="ubuntu", name="a1")
    assert [v["name"] for v in svc.list_vms(bob)] == []
    assert [v["name"] for v in svc.list_vms(admin)] == ["a1"]
    for action in (lambda: svc.get_vm(bob, "a1"),
                   lambda: svc.exec(bob, "a1", "id"),
                   lambda: svc.snapshot(bob, "a1", "s"),
                   lambda: svc.destroy(bob, "a1"),
                   lambda: svc.history(bob, "a1"),
                   lambda: svc.fork(bob, "a1", "s")):
        with pytest.raises(NotFound):
            action()
    # list_vms never leaks passwords
    assert "access" not in svc.list_vms(admin)[0]
    assert svc.get_vm(admin, "a1")["access"]["password"]


def test_quotas(env):
    svc, hv, net, caller = env
    small = caller("small", quota=Quota(max_vms=1, max_ram_mb=4096, max_vcpus=4))
    with pytest.raises(Forbidden, match="RAM"):
        svc.create_vm(small, image="win11")
    svc.create_vm(small, image="ubuntu")
    with pytest.raises(Forbidden, match="1/1 VMs"):
        svc.create_vm(small, image="ubuntu")


def test_failed_creation_cleans_up(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    hv.fail_create = True
    with pytest.raises(StrataError):
        svc.create_vm(alice, image="ubuntu", name="x1")
    assert svc.list_vms(alice) == [] and hv.dhcp == set() and net.forwards == []
    entry = svc.history(alice)[0]
    assert entry["action"] == "create_vm" and entry["ok"] == 0


def test_snapshot_revert_fork(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="ubuntu", name="a1")
    svc.snapshot(alice, "a1", "clean")
    with pytest.raises(Conflict):
        svc.snapshot(alice, "a1", "clean")
    assert svc.revert(alice, "a1", "clean")["reverted_to"] == "clean"
    fork = svc.fork(alice, "a1", "clean", name="a2")
    assert hv.vms["a2"]["spec"].backing == hv.vms["a1"]["snaps"]["clean"]
    assert fork["source"] == "snapshot a1/clean"
    # a snapshot or VM with forks cannot be removed from under them
    with pytest.raises(Conflict):
        svc.delete_snapshot(alice, "a1", "clean")
    with pytest.raises(Conflict):
        svc.destroy(alice, "a1")
    svc.destroy(alice, "a2")
    svc.delete_snapshot(alice, "a1", "clean")
    svc.destroy(alice, "a1")
    assert hv.vms == {} and net.forwards == []


def test_permanent_vms(env):
    svc, hv, net, caller = env
    alice, admin = caller("alice"), caller("root", admin=True)
    with pytest.raises(Forbidden):
        svc.create_vm(alice, image="ubuntu", permanent=True)
    svc.create_vm(alice, image="ubuntu", name="a1")
    with pytest.raises(Forbidden):
        svc.keep(alice, "a1", permanent=True)
    svc.keep(admin, "a1", permanent=True)
    with pytest.raises(Forbidden):
        svc.destroy(alice, "a1")
    # permanent VMs never expire
    svc.db.execute("UPDATE vms SET expires_at = 0")
    assert svc.reap_expired() == []
    svc.destroy(admin, "a1")


def test_expiry(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="ubuntu", name="a1")
    with pytest.raises(Forbidden):
        svc.keep(alice, "a1", hours=24 * 30)
    svc.keep(alice, "a1", hours=48)
    svc.db.execute("UPDATE vms SET expires_at = 1")
    assert svc.reap_expired() == ["a1"]
    assert hv.vms == {}


def test_internet_toggle(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="ubuntu", name="a1")
    ip = hv.vms["a1"]["spec"].ip
    svc.set_internet(alice, "a1", True)
    assert net.internet == [ip]
    svc.set_internet(alice, "a1", False)
    assert net.internet == []


def test_exec_and_files(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="win11", name="w1")
    out = svc.exec(alice, "w1", "Get-Date")
    assert out["exit_code"] == 0 and out["stdout"].startswith("powershell.exe")
    assert svc.exec(alice, "w1", "ver", shell="cmd")["stdout"].startswith("cmd.exe /c ver")
    with pytest.raises(StrataError, match="unknown shell"):
        svc.exec(alice, "w1", "id", shell="bash")
    svc.upload(alice, "w1", r"C:\tools\a.exe", b"MZ\x90\x00" * 10)
    assert svc.download(alice, "w1", r"C:\tools\a.exe") == b"MZ\x90\x00" * 10
    # file contents never end up in the journal
    upload = next(e for e in svc.history(alice, "w1") if e["action"] == "upload")
    assert "MZ" not in upload["params"]
    svc.power(alice, "w1", "stop")
    with pytest.raises(Conflict, match="shutoff"):
        svc.exec(alice, "w1", "Get-Date")


def test_click_bounds(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="win11", name="w1")
    svc.click(alice, "w1", 10, 10)
    with pytest.raises(StrataError, match="outside"):
        svc.click(alice, "w1", 5000, 10)


def test_names_validated(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    for bad in ("UPPER", "a/b", "../x", "x" * 40, "-x", "a1\n"):
        with pytest.raises(StrataError):
            svc.create_vm(alice, image="ubuntu", name=bad)
    svc.create_vm(alice, image="ubuntu", name="a1")
    with pytest.raises(StrataError):
        svc.snapshot(alice, "a1", "bad name")


def test_diff(env):
    svc, hv, net, caller = env
    alice = caller("alice")
    svc.create_vm(alice, image="ubuntu", name="a1")
    svc.snapshot(alice, "a1", "before")
    svc.snapshot(alice, "a1", "after")
    out = svc.diff(alice, "a1", "before", "after")
    assert out["total"] == 1 and out["counts"]["added"] == 1
    assert out["changes"][0]["path"] == "/tmp/new"
    with pytest.raises(NotFound):
        svc.diff(alice, "a1", "before", "missing")


def test_generated_passwords_meet_windows_complexity():
    from strata.service import gen_password
    seen = set()
    for _ in range(200):
        pw = gen_password()
        assert len(pw) == 16
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
        seen.add(pw)
    assert len(seen) > 190  # effectively always unique


def test_addresses_and_ports_are_unique_across_vms(env):
    svc, hv, net, caller = env
    admin = caller("root", admin=True)
    ips, ports, macs = set(), set(), set()
    for i in range(8):
        vm = svc.create_vm(admin, image="win11", name=f"w{i}")
        ip = hv.vms[vm["name"]]["spec"].ip
        assert ip not in ips and ip.startswith("10.99.0.")
        ips.add(ip)
        # ssh and rdp ports distinct and within the configured range
        for pair in net.forwards:
            assert 30000 <= pair[0] <= 30999
            assert pair[0] not in ports
            ports.add(pair[0])
        ports.clear()
        macs.add(hv.vms[vm["name"]]["spec"].mac)
    assert len(ips) == 8 and len(macs) == 8


def test_delete_image(env):
    svc, hv, net, caller = env
    alice, admin = caller("alice"), caller("root", admin=True)
    # non-admin cannot delete an image
    with pytest.raises(Forbidden):
        svc.delete_image(alice, "ubuntu")
    # refused while a VM from the image exists
    svc.create_vm(alice, image="ubuntu", name="u1")
    with pytest.raises(Conflict, match="still use"):
        svc.delete_image(admin, "ubuntu")
    svc.destroy(alice, "u1")
    # now it deletes and the volume is removed
    out = svc.delete_image(admin, "ubuntu")
    assert out["deleted"] == "ubuntu" and out["volume_removed"]
    assert "ubuntu" in getattr(hv, "deleted_volumes", set()) or "ubuntu.qcow2" in getattr(hv, "deleted_volumes", set())
    assert [i["name"] for i in svc.list_images(admin)] == ["win11"]
    with pytest.raises(NotFound):
        svc.delete_image(admin, "ubuntu")

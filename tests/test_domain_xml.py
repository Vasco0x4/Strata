"""The VM definition is where 'minimal hardware, nothing leaves' is enforced. We build the
domain XML with libvirt faked and assert the security contract on it, so a future edit that
adds a USB controller, a shared folder, a clipboard channel, or opens the VNC socket breaks
a test. Also covers the virt-diff parser and the overlay-free pure helpers.
"""

import sys
import types
import xml.etree.ElementTree as ET

import pytest


@pytest.fixture
def hv_mod(monkeypatch):
    for mod in ("libvirt", "libvirt_qemu", "guestfs"):
        m = types.ModuleType(mod)
        m.__getattr__ = lambda name: 0  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, mod, m)
    import importlib
    hv = importlib.import_module("strata.libvirt_hv")
    return importlib.reload(hv)


def build(hv_mod, os_="linux"):
    from strata.config import Config
    from strata.hypervisor import VMSpec
    spec = VMSpec(name="t1", os=os_, vcpus=2, ram_mb=4096, mac="52:54:00:ab:cd:ef",
                  ip="10.99.0.10", backing="/srv/strata/strata-images/base.qcow2")
    xml = hv_mod._domain_xml(Config(), spec, "/srv/strata/strata-vms/t1.0.qcow2")
    return spec, ET.fromstring(xml)


def test_no_dangerous_devices(hv_mod):
    _, dom = build(hv_mod)
    devices = dom.find("devices")
    # No way for data to cross out of the VM except the screen and the guest agent.
    usb = devices.find("controller[@type='usb']")
    assert usb is not None and usb.get("model") == "none", "USB controller must be disabled"
    assert devices.find("filesystem") is None, "no shared folder"
    assert devices.find("redirdev") is None, "no USB redirection"
    assert devices.find("sound") is None and devices.find("audio") is None, "no audio"
    assert devices.find("memballoon").get("model") == "none"
    # Only the guest-agent channel is allowed; no spice/clipboard agent channels.
    for ch in devices.findall("channel"):
        assert ch.find("target").get("name") == "org.qemu.guest_agent.0", "unexpected channel"


def test_vnc_has_no_listening_socket(hv_mod):
    _, dom = build(hv_mod)
    g = dom.find("devices/graphics")
    assert g.get("type") == "vnc"
    assert g.find("listen").get("type") == "none", "VNC must not listen on a socket"
    assert g.get("listen") not in ("0.0.0.0", "::"), "VNC must not bind an address"
    assert dom.find("devices/graphics[@type='spice']") is None, "no SPICE"


def test_nic_is_antispoof_pinned(hv_mod):
    spec, dom = build(hv_mod)
    iface = dom.find("devices/interface")
    assert iface.find("source").get("network") == "strata"
    assert iface.find("mac").get("address") == spec.mac
    fref = iface.find("filterref")
    assert fref is not None and fref.get("filter") == "clean-traffic", "missing anti-spoof filter"
    learn = fref.find("parameter[@name='CTRL_IP_LEARNING']")
    assert learn is not None and learn.get("value") == "dhcp", "NIC anti-spoof not set to DHCP learning"


def test_resources_match_spec(hv_mod):
    spec, dom = build(hv_mod)
    assert dom.find("memory").text == str(spec.ram_mb)
    assert dom.find("vcpu").text == str(spec.vcpus)
    assert dom.find("name").text == "strata-t1"
    assert dom.find("devices/disk/source").get("file") == "/srv/strata/strata-vms/t1.0.qcow2"


def test_windows_gets_tpm_and_secure_boot(hv_mod):
    _, dom = build(hv_mod, "windows")
    assert dom.find("devices/tpm") is not None, "Windows 11 needs a TPM"
    assert dom.find("os").get("firmware") == "efi"
    features = {f.get("name") for f in dom.findall("os/firmware/feature") if f.get("enabled") == "yes"}
    assert {"secure-boot", "enrolled-keys"} <= features, "Windows 11 needs Secure Boot with enrolled keys"
    assert dom.find("features/smm").get("state") == "on", "Secure Boot needs SMM to protect the var store"


def test_linux_has_no_tpm_or_secureboot(hv_mod):
    _, dom = build(hv_mod)
    assert dom.find("devices/tpm") is None
    assert dom.find("os/firmware") is None, "Linux VMs should not request Secure Boot firmware"


def test_parse_virt_diff(hv_mod):
    sample = """\
+ - 0644        12 /etc/newfile
- d 0755         0 /var/gone
= - 0644       100 /unchanged-should-be-ignored-if-marked
Files /a and /b differ
~ - 0644       200 /etc/changed"""
    out = hv_mod.LibvirtHypervisor._parse_virt_diff(sample)
    kinds = {c["path"]: c["change"] for c in out}
    assert kinds.get("/etc/newfile") == "added"
    assert kinds.get("/var/gone") == "deleted"
    # lines that are not real change markers must be skipped, not crash
    assert "/a" not in kinds and "and" not in str(out)

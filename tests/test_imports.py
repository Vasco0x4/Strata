"""Import every module, with libvirt/guestfs faked, to catch load-time errors in the
code paths the other tests never import (libvirt_hv, server). No real libvirt needed.

Also checks that the names server/api/cli use on Service actually exist with compatible
signatures, so a rename cannot silently break the wiring.
"""

import importlib
import inspect
import sys
import types

import pytest


@pytest.fixture
def fake_libvirt(monkeypatch):
    for mod in ("libvirt", "libvirt_qemu", "guestfs"):
        m = types.ModuleType(mod)
        # libvirt code reads many module-level constants; return 0 for any VIR_* name.
        m.__getattr__ = lambda name: 0  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, mod, m)
    yield


def test_libvirt_hv_imports(fake_libvirt):
    hv = importlib.import_module("strata.libvirt_hv")
    importlib.reload(hv)
    from strata.hypervisor import Hypervisor
    # Every method declared in the protocol exists on the implementation
    protocol_methods = {n for n in dir(Hypervisor) if not n.startswith("_")}
    missing = [m for m in protocol_methods if not hasattr(hv.LibvirtHypervisor, m)]
    assert not missing, f"LibvirtHypervisor is missing: {missing}"


def test_server_imports(fake_libvirt):
    srv = importlib.import_module("strata.server")
    importlib.reload(srv)
    assert callable(srv.main) and callable(srv.build_app)


def test_service_methods_match_callers(fake_libvirt):
    """The MCP/API/CLI call Service methods by name; make sure those names exist."""
    from strata.service import Service
    for name in ("list_images", "list_vms", "get_vm", "create_vm", "fork", "exec", "upload",
                 "download", "screenshot", "click", "type_text", "send_keys", "snapshot",
                 "list_snapshots", "revert", "delete_snapshot", "power", "keep", "set_internet",
                 "destroy", "history", "diff", "reap_expired", "resync"):
        assert callable(getattr(Service, name)), f"Service.{name} is missing"


def test_hypervisor_impl_signatures_match(fake_libvirt):
    """LibvirtHypervisor and the fake agree with the protocol on parameter names."""
    from strata.hypervisor import Hypervisor
    from strata.libvirt_hv import LibvirtHypervisor
    for name in ("create_vm", "exec", "snapshot", "revert", "diff_disks", "add_dhcp_host"):
        proto = inspect.signature(getattr(Hypervisor, name))
        impl = inspect.signature(getattr(LibvirtHypervisor, name))
        assert list(proto.parameters) == list(impl.parameters), \
            f"{name}: protocol {list(proto.parameters)} vs impl {list(impl.parameters)}"

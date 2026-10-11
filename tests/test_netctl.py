import importlib.machinery
import importlib.util
import ipaddress
from pathlib import Path

import pytest

path = Path(__file__).parent.parent / "helpers" / "strata-netctl"
loader = importlib.machinery.SourceFileLoader("strata_netctl", str(path))
spec = importlib.util.spec_from_loader("strata_netctl", loader)
netctl = importlib.util.module_from_spec(spec)
loader.exec_module(netctl)

SUBNET = ipaddress.IPv4Network("10.99.0.0/24")


def build(req):
    return netctl.build_script(req, SUBNET, 30000, 30999)


def test_valid_request():
    script = build({"forwards": [[30000, "10.99.0.10", 22], [30001, "10.99.0.10", 3389]],
                    "internet": ["10.99.0.10"]})
    assert "flush map inet strata port_forwards" in script
    assert "add element inet strata port_forwards { 30000 : 10.99.0.10 . 22, 30001 : 10.99.0.10 . 3389 }" in script
    assert "add element inet strata internet_vms { 10.99.0.10 }" in script


def test_empty_request_flushes():
    script = build({"forwards": [], "internet": []})
    assert script.count("flush") == 2 and "add element" not in script


@pytest.mark.parametrize("req", [
    {"forwards": [[22, "10.99.0.10", 22]], "internet": []},             # host port out of range
    {"forwards": [[30000, "10.50.0.1", 22]], "internet": []},           # not a VM address
    {"forwards": [[30000, "10.99.0.1", 22]], "internet": []},           # the gateway (the host)
    {"forwards": [[30000, "10.99.0.10", 8006]], "internet": []},        # VM port not allowed
    {"forwards": [[30000, "10.99.0.10", 22], [30000, "10.99.0.11", 22]], "internet": []},
    {"forwards": [[30000, "10.99.0.10 } ; flush ruleset ; {", 22]], "internet": []},
    {"forwards": [[True, "10.99.0.10", 22]], "internet": []},
    {"forwards": [], "internet": ["192.168.1.1"]},
    {"forwards": [], "internet": [], "extra": 1},
    {"forwards": "x", "internet": []},
])
def test_invalid_requests_refused(req):
    with pytest.raises(Exception):
        build(req)

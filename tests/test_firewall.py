"""The firewall is the core of 'nothing leaves a VM'. We cannot load real nft rules here
(no privileges), so we render the template as install-host.sh does and assert the security
contract on the result. These checks are meant to catch a future edit that opens a hole.
"""

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "setup" / "firewall.nft.in"


def render(lan="ens18", bridge="strata0", build="stratab0", subnet="10.99.0.0/24", build_subnet="10.99.1.0/24"):
    text = TEMPLATE.read_text()
    for a, b in [("@LAN_IF@", lan), ("@BRIDGE@", bridge), ("@BUILD_BRIDGE@", build),
                 ("@SUBNET@", subnet), ("@BUILD_SUBNET@", build_subnet)]:
        text = text.replace(a, b)
    assert not re.search(r"@[A-Z_]+@", text), "unrendered placeholder left"
    return text


@pytest.fixture
def fw():
    return render()


def line_with(text, *needles):
    for line in text.splitlines():
        s = line.strip()
        if all(n in s for n in needles):
            return s
    return None


def test_vm_egress_defaults_to_drop(fw):
    # The forward chain must end any VM-bridge path with an explicit drop (set form covers it).
    in_drop = line_with(fw, "iifname", "strata0", "drop")
    out_drop = line_with(fw, "oifname", "strata0", "drop")
    assert in_drop and "accept" not in in_drop, "no catch-all drop for VM egress"
    assert out_drop and "accept" not in out_drop, "no catch-all drop toward VMs"
    # The drops must come after the accepts in the forward chain, or allowed traffic never passes.
    fwd_start = fw.index("hook forward")
    fwd = fw[fwd_start:fw.index("chain prerouting")]
    assert fwd.rindex("drop") > fwd.rindex("accept"), "a drop precedes the last accept"


def test_host_only_reachable_for_dhcp(fw):
    # Input from a VM: DHCP accepted, everything else dropped.
    dhcp = line_with(fw, '"strata0"', "udp dport 67", "accept")
    drop = line_with(fw, 'iifname { "strata0", "stratab0" }', "drop")
    assert dhcp and drop
    # and the drop must be able to fire (present in the input hook)
    assert "hook input" in fw


def test_internet_only_with_allowlist_and_never_private(fw):
    rule = line_with(fw, 'iifname "strata0"', "@internet_vms", "accept")
    assert rule, "VM internet rule missing"
    assert "ip daddr != @private_nets" in rule, "VM internet rule could reach private networks"
    assert "@internet_vms" in rule, "VM internet rule not gated by the allow-list"


def test_build_network_has_internet_but_no_private(fw):
    rule = line_with(fw, 'iifname "stratab0"', "accept")
    assert rule and "ip daddr != @private_nets" in rule, "build network could reach private networks"


def test_no_rule_bridges_vms_to_each_other(fw):
    # There must be no accept whose in- and out-interface are both the VM bridge.
    for line in fw.splitlines():
        s = line.strip()
        if "accept" in s and 'iifname "strata0"' in s and 'oifname "strata0"' in s:
            pytest.fail(f"rule lets VMs talk to each other: {s}")


def test_masquerade_limited_to_lab_subnets(fw):
    masq = line_with(fw, "masquerade")
    assert masq and "10.99.0.0/24" in masq and "10.99.1.0/24" in masq
    assert 'oifname "ens18"' in masq, "masquerade not pinned to the LAN interface"


def test_private_nets_cover_the_homelab(fw):
    # The host lives on 10.50.0.0/16; make sure 10/8 is in the blocked set.
    block = fw[fw.index("private_nets"):]
    for net in ("10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12", "169.254.0.0/16"):
        assert net in block, f"{net} not in private_nets"

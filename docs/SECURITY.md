# Security model

Strata runs code you do not trust inside VMs. This is the threat model and the layers
that defend against it. The guiding rule: **nothing leaves a VM unless you explicitly
allow it, and no single bug should be enough to reach the host or your network.**

## What Strata defends

The host, the local network, the parent hypervisor (e.g. Proxmox) and other VMs, against
code running inside a VM that is assumed hostile.

## What Strata does not defend against

- A VM attacking a service you deliberately expose to it (if you turn on internet, the VM
  can reach the internet).
- The content a VM returns. Command output, files and screenshots are **data**. Strata
  size-limits them and never interprets them; agents and users must do the same and never
  follow instructions found in VM output.
- A compromise of the host itself through a channel outside Strata (your SSH, the Proxmox
  layer). Strata assumes the host is reasonably maintained.
- Guarant­eeing zero hypervisor escapes. No one can. Strata makes one escape insufficient.

## The layers

Reaching your Proxmox from inside a VM requires chaining several independent bugs:

1. **The VM cannot address anything.** The VM network (`strata`) is isolated: VMs cannot
   talk to each other (`<port isolated>`), and the host firewall (`inet strata`) drops
   everything leaving a VM except DHCP to the host. Internet is opt-in per VM and the rule
   that permits it also forbids every private network (`ip daddr != @private_nets`), so an
   "internet" VM still cannot reach 10/8, 192.168/16, 172.16/12 or link-local. The NIC
   carries the `clean-traffic` filter pinned to the VM's IP, so it cannot spoof another
   address.
2. **Fail-closed.** `strata-firewall.service` is a hard dependency of the libvirt daemons:
   if the firewall does not load, no VM starts. There is no window where VMs run without it.
3. **Minimal attack surface.** VMs get virtio devices only: no USB controller, no sound,
   no 3D, no shared folders, no clipboard channel, no SPICE. The screen is a VNC display
   with no listening socket (libvirt hands the service a connected FD for the web console).
   Fewer devices, less emulator code an escape can target. These properties are asserted by
   tests (`tests/test_domain_xml.py`).
4. **QEMU is confined.** It runs as the unprivileged `libvirt-qemu` user under an AppArmor
   profile and a seccomp filter. An escape from the guest lands in a confined process on
   the lab host, not on Proxmox. Kernel and QEMU security updates are automatic.
5. **The service is not root.** `stratad` runs as `strata` with no capabilities, a
   read-only system, and a syscall allow-list (see the systemd unit). It exposes only its
   own actions — never raw libvirt XML or host paths. The one privileged operation,
   updating firewall port-forwards, goes through a tiny root helper
   (`helpers/strata-netctl`) that validates every value against fixed ranges and can only
   add forwards to VM addresses on allowed ports (tests in `tests/test_netctl.py`).
6. **Least authority for callers.** Each agent has its own token (stored as a SHA-256
   hash, revocable, with quotas and optional expiry) and sees only its own VMs. Tokens are
   re-checked on every call, so revocation is immediate.

## Recommended host placement

Run the Strata host as a VM on an isolated VLAN on your Proxmox, with Proxmox firewall
rules that allow only the ports you use (the web UI, and the RDP/SSH forwards) from your
workstation, and nothing from the Strata host toward the rest of your network or the
Proxmox management interface. Take a Proxmox snapshot of the host before first use. Then
even a full compromise of the lab host reaches nothing else.

## Known gaps to close on first live run

- The libvirt layer, the image builders and the noVNC console have not yet run on a real
  host; they are covered only by tests against a fake hypervisor.
- The `virt-diff` output parser is best-effort and needs validation against real output.
- Windows Defender stays on in the clean image; disabling it to study payloads is an
  opt-in, per-VM action (`images/windows/disable-defender.ps1`), never in the base image.

## Reporting

This is a personal project. Open an issue at github.com/Vasco0x4/Strata.

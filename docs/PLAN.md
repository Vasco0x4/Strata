# Plan

## Goal

A lab of virtual machines on one Linux host, driven by a single service.
A human (browser, CLI) and AI agents (MCP, one token each) use it from the local
network to create, drive, save, restore and compare Windows and Linux VMs, disposable
or long-lived. Nothing leaves a VM without explicit consent.

## Done when

1. An agent, with a single pasted `claude mcp add ...` line, creates a Windows 11 VM,
   runs a command in it, uploads a file, takes a snapshot, reverts, and forks a new VM
   from that snapshot. Every action returns one clear result.
2. The human sees all of it in the web UI (live screen, snapshot tree, journal) and can
   reach the VMs over RDP and SSH.
3. Isolation tests pass: from a VM, the host, the LAN, the parent hypervisor, other VMs
   and the internet (unless enabled for that VM) are all unreachable.
4. Tokens are created and revoked from the web UI; an agent only sees its own VMs.

## Security rules

- Fail-closed firewall: if the Strata firewall does not load, libvirt does not start.
- Everything leaving a VM is dropped, except DHCP to the host and the internet when
  enabled for that VM. Never to a private network.
- Minimal virtual hardware: virtio only, no USB, sound, 3D, shared folders or shared
  clipboard. Anti-spoofing filter (clean-traffic) on every NIC.
- QEMU confined: unprivileged user, AppArmor, seccomp. Automatic security updates.
- The service never runs as root and only exposes its own actions (no raw XML, no host
  paths). Anything coming out of a VM is untrusted data with a size limit.
- Tokens stored hashed. No credentials for other machines on the host.

## Steps

0. **Prepare (human)**: change the password, dedicated VLAN or Proxmox firewall rules for
   the host, Proxmox snapshot of the host.
1. **Host**: `setup/install-host.sh` (packages, disk, firewall, isolated network, service
   account). Test: `setup/smoke-test.sh` — a Linux VM cannot reach anything it shouldn't.
2. **Images**: automated builds of Windows 11 (evaluation) and Ubuntu.
3. **Core**: thin-copy VMs, snapshots (with or without memory), forks from a snapshot,
   disposable or permanent VMs, journal, quotas, tokens.
4. **Access**: MCP, API, CLI. RDP/SSH port openings through a small, strict root helper.
5. **Web UI**: live screen, snapshot tree, journal, tokens, file upload.
6. **Diff** between two states (files, Windows registry).
7. **Later**: more Linux images, multi-VM labs.
8. **Lockdown**: remove build-time access, audit, replay isolation tests.

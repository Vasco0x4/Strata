# Strata

Disposable and long-lived virtual machines for humans and AI agents, on a single
Linux host with KVM.

Start from a clean image, copy it instantly, run anything inside, snapshot the
interesting states, go back, fork from any state, and see what changed between two
states. Agents drive it through MCP with their own token; humans use a web UI or the CLI.

> **Status:** early work in progress. See [docs/PLAN.md](docs/PLAN.md).

## Security model

Strata assumes everything inside a VM is hostile.

- Nothing leaves a VM by default: no route to the host, the LAN or other VMs.
  Internet access is opt-in per VM and never reaches private networks.
- The firewall is fail-closed: without it, libvirt does not start.
- VMs get minimal virtio hardware, no shared folders, clipboard or USB.
- QEMU runs unprivileged, confined by AppArmor and seccomp.
- The Strata service never runs as root and only exposes its own actions.

No hypervisor is bug-free. Strata layers defenses so that one bug is not enough, and
works best on a host that is itself a VM on an isolated network.

## Install (host)

Requirements: Ubuntu 24.04 or later, `/dev/kvm` available.

```bash
sudo bash setup/install-host.sh
```

## License

[MIT](LICENSE)

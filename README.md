# Strata

Disposable and long-lived virtual machines for humans and AI agents, on a single
Linux host with KVM.

Start from a clean image, copy it instantly, run anything inside, snapshot the
interesting states, go back, fork from any state, and see what changed between two
states. Agents drive it through MCP with their own token; humans use a web UI or the CLI.

> **Security model:** see [docs/SECURITY.md](docs/SECURITY.md).
>
> **Status:** running on a real KVM host. The full acceptance + isolation suite
> (`setup/smoke-test.sh`) passes: create, exec, file transfer, snapshot, diff, revert,
> fork, and a VM that cannot reach the host, the LAN, the hypervisor or the internet
> unless explicitly allowed. Windows 11 images and the web console's live screen are the
> next things to exercise on real hardware. See [docs/PLAN.md](docs/PLAN.md).

## Security model

Strata assumes everything inside a VM is hostile.

- Nothing leaves a VM by default: no route to the host, the LAN, the parent hypervisor
  or other VMs. Internet access is opt-in per VM and never reaches a private network.
- The firewall is fail-closed: without it, libvirt does not start.
- VMs get minimal virtio hardware: no shared folders, clipboard or USB.
- QEMU runs unprivileged, confined by AppArmor and seccomp; security updates are automatic.
- The Strata service never runs as root and only exposes its own actions. Anything coming
  out of a VM is treated as untrusted data.

No hypervisor is bug-free. Strata layers defenses so that one bug is not enough, and is
meant to run on a host that is itself a VM on an isolated network.

## Getting started

On the host (Ubuntu 24.04+ with `/dev/kvm`):

```bash
# 1. Prepare the host: packages, fail-closed firewall, isolated networks, service account.
#    Read it first, then run it. It asks for your sudo password.
sudo bash setup/install-host.sh

# 2. Deploy the service (as your normal user; needs internet once for dependencies).
bash setup/deploy.sh

# 3. Build at least one image (as your user; takes a while).
images/linux/build-ubuntu.sh 24.04
images/windows/build-windows11.sh        # optional, downloads the eval ISO
```

The admin token is written to `/var/lib/strata/admin-token`:

```bash
sudo cat /var/lib/strata/admin-token
```

Open `http://<host>:8080`, paste that token, and create an agent on the **Agents** page.
Each agent gets a ready-to-paste line:

```bash
claude mcp add --transport http strata http://<host>:8080/mcp --header "Authorization: Bearer strata_…"
```

Verify the whole thing — core actions and, above all, that a VM cannot reach the host,
the LAN, the hypervisor or the internet (set `LAN_PROBE` to an address on your LAN):

```bash
LAN_PROBE=10.50.0.1 bash setup/smoke-test.sh
```

Once that passes, remove the build-time access:

```bash
sudo bash setup/lockdown.sh
```

## Using it

- **Agents** call MCP tools: `create_vm`, `exec`, `upload`/`download`, `screenshot` with
  `click`/`type_text`, `snapshot`/`revert`/`fork`, `diff`, `set_internet`, `destroy`.
- **The CLI** (`strata`) does the same from a shell; on the host it uses the local admin
  socket with no setup, elsewhere `strata login <url> <token>`.
- **The web UI** shows the live screen, the snapshot tree, the journal, state comparison,
  and agent-token management.

## License

[MIT](LICENSE)

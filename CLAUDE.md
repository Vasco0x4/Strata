# Strata — notes for Claude

A lab of KVM/libvirt VMs (Windows 11, Linux) driven by one service, for a human (web UI,
CLI) and AI agents (MCP, one token each) on the LAN. Disposable or long-lived VMs,
instant copies, snapshots, forks from any state, offline diff between states.

## Layout
- `src/strata/` the service: `config`, `db` (SQLite), `auth` (hashed tokens), `service`
  (all actions, permission-checked + journaled — the only module MCP/API/CLI call),
  `hypervisor` (Protocol) + `libvirt_hv` (real impl), `netctl` (client of the root
  firewall helper), `mcp_tools`, `api` (Starlette), `cli`, `server` (stratad), `web/`.
- `setup/` host install (`install-host.sh`), `deploy.sh`, `lockdown.sh`, firewall and
  network templates, the root firewall helper `helpers/strata-netctl`.
- `images/` builders for Ubuntu and Windows 11 (official media, verified).
- `tests/` run with `.venv/bin/python -m pytest` — a fake hypervisor, plus a real
  end-to-end MCP/HTTP/CLI test. No libvirt needed.

## Conventions
- Repo content in English; talk to the user (Vasco0x4) in French.
- Security is a hard rule: nothing leaves a VM unless explicitly allowed; service never
  root; VM output is untrusted. Keep it that way in every change.
- Tooling: `uv` and `node` are in `~/.local/bin` (not system). Venv: `~/strata/.venv`.
- Commit in English, push to github.com/Vasco0x4/Strata. Test, commit and push per step.

## State / next
LIVE on the real KVM host (installed via the user's sudo; password was sandbox/sandbox —
do not store it). stratad runs on http://10.50.0.214:8080; admin token at
/var/lib/strata/admin-token. `setup/smoke-test.sh` passes 14/14: create, exec, files,
snapshot, diff, revert, fork, and full isolation (VM cannot reach host/LAN/Proxmox/internet
unless enabled). 63 tests + ruff + CI green.

Host specifics discovered live (baked into install-host.sh): libvirtd is the monolithic
daemon (not virtqemud); qemu.conf has dynamic_ownership=1 (libvirt chowns disks and the
swtpm socket to qemu — Windows 11 TPM needs it), remember_owner=0, namespaces=[];
pools are setgid kvm 2775; strata user is in kvm+libvirt+strata-admin. IPs are NOT pinned —
dnsmasq wouldn't honor reservations, so VMs get sticky DHCP leases and Strata discovers the
IP via the guest agent; clean-traffic uses CTRL_IP_LEARNING=dhcp. Offline diff uses a
prebuilt fixed libguestfs appliance (/var/lib/strata/appliance) so it needn't read /boot.
To run CLI as yourself: `sudo -u sandbox` (fresh shell picks up the kvm/strata-admin groups);
`strata` is symlinked at /usr/local/bin.

Next: build the Windows 11 image (images/windows/build-windows11.sh) and exercise it;
visually confirm the noVNC live console in a browser; then setup/lockdown.sh. See docs/PLAN.md.

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
The host installer has not been run yet (no libvirt on this box). Everything is written
and passes against the fake hypervisor. The live bring-up — register images, create a
real Win11/Ubuntu VM, run the isolation tests, exercise the libvirt layer and the noVNC
console — is pending the user running `setup/install-host.sh`. See `docs/PLAN.md`.

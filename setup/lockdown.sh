#!/usr/bin/env bash
# Final lockdown: remove the build-time access install-host.sh granted, so from now on
# agents and the UI reach the hypervisor only through the Strata service. Run with sudo.
set -euo pipefail

DEV_USER="${DEV_USER:-${SUDO_USER:-}}"
APP_DIR=/opt/strata

[[ $EUID -eq 0 ]] || { echo "run it with sudo"; exit 1; }

echo "==> revoking build-time access for '$DEV_USER'"
if [[ -n "$DEV_USER" && "$DEV_USER" != root ]]; then
	gpasswd -d "$DEV_USER" libvirt 2>/dev/null || true
	gpasswd -d "$DEV_USER" strata-admin 2>/dev/null || true
fi
rm -f /etc/polkit-1/rules.d/50-strata-build.rules
chown -R root:root "$APP_DIR" 2>/dev/null || true

echo "==> re-checking isolation and sandboxing"
fails=0
check() { if "$@" >/dev/null 2>&1; then printf '  [OK]   %s\n' "$1"; else printf '  [FAIL] %s\n' "$1"; fails=$((fails+1)); fi; }
nft list table inet strata >/dev/null 2>&1 && echo "  [OK]   firewall loaded" || { echo "  [FAIL] firewall"; fails=$((fails+1)); }
systemctl is-active -q stratad && echo "  [OK]   stratad running" || { echo "  [FAIL] stratad"; fails=$((fails+1)); }
id "$DEV_USER" 2>/dev/null | grep -q 'libvirt' && { echo "  [FAIL] $DEV_USER still in libvirt (log out/in to apply)"; } || echo "  [OK]   $DEV_USER no longer in libvirt"
grep -rq "strata-build" /etc/polkit-1/rules.d/ 2>/dev/null && { echo "  [FAIL] build polkit rule still present"; fails=$((fails+1)); } || echo "  [OK]   build polkit rule removed"

echo
echo "Lockdown done. Group changes take effect on your next login."
echo "Verify from inside a VM that the host, the LAN and the internet are unreachable:"
echo "  strata create ubuntu --name probe && strata exec probe -- 'curl -m5 http://10.99.0.1 || echo blocked'"
(( fails == 0 )) || { echo "$fails check(s) failed."; exit 1; }

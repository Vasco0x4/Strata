#!/usr/bin/env bash
# Deploy the Strata code into /opt/strata and (re)start the service.
# Run as the user install-host.sh granted build access to (in the libvirt group),
# NOT as root. Needs internet once, to fetch Python dependencies.
set -euo pipefail

APP_DIR=/opt/strata
REPO="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -ne 0 ]] || { echo "run this as your normal user, not root"; exit 1; }
[[ -w "$APP_DIR" ]] || { echo "$APP_DIR is not writable by $USER; run setup/install-host.sh first"; exit 1; }

echo "==> building the virtualenv in $APP_DIR/.venv"
# Idempotent: create the venv only if missing, then always (re)install the code.
if command -v uv >/dev/null; then
	[[ -x "$APP_DIR/.venv/bin/python" ]] || \
		uv venv --system-site-packages --python "$(command -v python3)" "$APP_DIR/.venv"
	VIRTUAL_ENV="$APP_DIR/.venv" uv pip install --reinstall-package strata "$REPO"
else
	[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv --system-site-packages "$APP_DIR/.venv"
	"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
	"$APP_DIR/.venv/bin/pip" install --force-reinstall --no-deps "$REPO"
	"$APP_DIR/.venv/bin/pip" install "$REPO"
fi

# The service needs python3-libvirt and python3-guestfs from system site-packages.
"$APP_DIR/.venv/bin/python" -c "import libvirt, guestfs" 2>/dev/null \
	|| echo "WARNING: python3-libvirt / python3-guestfs not visible; is the venv --system-site-packages?"

echo "==> (re)starting stratad"
systemctl restart stratad.service
sleep 2
if systemctl is-active -q stratad.service; then
	echo "Strata is running."
	token=/var/lib/strata/admin-token
	if sudo test -f "$token" 2>/dev/null || [[ -r "$token" ]]; then
		echo "Admin token: sudo cat $token"
	fi
	echo "Open the web UI at the address in: journalctl -u stratad -n 20 --no-pager | grep 'Strata'"
else
	echo "stratad did not start. See: journalctl -u stratad -n 40 --no-pager"
	exit 1
fi

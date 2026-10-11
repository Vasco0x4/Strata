#!/usr/bin/env bash
# Prepares a Linux host for Strata. Run once with sudo; safe to run again.
#
# What it does:
#   1. installs the Strata firewall and makes libvirt depend on it (no firewall, no VMs)
#   2. installs QEMU/KVM, libvirt, OVMF, swtpm, libguestfs
#   3. creates the isolated VM network and the image-build network (internet only),
#      and stops libvirt's "default" network
#   4. grows the root LVM volume into free space (EXTEND_LVM=no to skip)
#   5. creates the "strata" service account, its config and storage pools
#   6. installs the root helper that updates the firewall (the service never runs as root)
#   7. installs the Strata service unit (sandboxed, starts once the code is deployed)
#   8. gives the user building Strata temporary access (removed by setup/lockdown.sh)
#   9. checks everything
#
# Settings (environment variables):
#   LAN_IF, BRIDGE, SUBNET_PREFIX, BUILD_BRIDGE, BUILD_PREFIX, FORWARD_PORTS, DEV_USER, EXTEND_LVM
set -euo pipefail

LAN_IF="${LAN_IF:-$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')}"
BRIDGE="${BRIDGE:-strata0}"
SUBNET_PREFIX="${SUBNET_PREFIX:-10.99.0}"
BUILD_BRIDGE="${BUILD_BRIDGE:-stratab0}"
BUILD_PREFIX="${BUILD_PREFIX:-10.99.1}"
FORWARD_PORTS="${FORWARD_PORTS:-30000-30999}"
DEV_USER="${DEV_USER:-${SUDO_USER:-}}"
EXTEND_LVM="${EXTEND_LVM:-yes}"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
CONF_DIR=/etc/strata
DATA_DIR=/srv/strata
APP_DIR=/opt/strata

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run it with sudo"
[[ -n "$LAN_IF" ]] && ip link show "$LAN_IF" >/dev/null 2>&1 || die "LAN interface not found (LAN_IF=$LAN_IF)"
[[ -e /dev/kvm ]] || die "/dev/kvm missing: no (nested) virtualization available"
[[ "$FORWARD_PORTS" =~ ^[0-9]+-[0-9]+$ ]] || die "FORWARD_PORTS must look like 30000-30999"
LAN_IP=$(ip -4 -o addr show dev "$LAN_IF" | awk '{print $4}' | cut -d/ -f1 | head -n1)

render() {  # render <template> <destination> [network name] [bridge] [prefix]
	sed -e "s|@LAN_IF@|$LAN_IF|g" -e "s|@BRIDGE@|$BRIDGE|g" -e "s|@BUILD_BRIDGE@|$BUILD_BRIDGE|g" \
	    -e "s|@SUBNET@|$SUBNET_PREFIX.0/24|g" -e "s|@BUILD_SUBNET@|$BUILD_PREFIX.0/24|g" \
	    -e "s|@NET_NAME@|${3:-}|g" -e "s|@NET_BRIDGE@|${4:-}|g" -e "s|@NET_PREFIX@|${5:-}|g" \
	    "$1" > "$2"
}

step "1/9 Firewall"
install -d -m 0755 "$CONF_DIR"
render "$HERE/firewall.nft.in" "$CONF_DIR/firewall.nft"
nft -c -f "$CONF_DIR/firewall.nft"
cat > /etc/systemd/system/strata-firewall.service <<'EOF'
[Unit]
Description=Strata VM firewall
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/strata/firewall.nft

[Install]
WantedBy=multi-user.target
EOF
for unit in libvirtd virtqemud virtnetworkd; do
	install -d "/etc/systemd/system/$unit.service.d"
	cat > "/etc/systemd/system/$unit.service.d/strata-firewall.conf" <<'EOF'
[Unit]
# No Strata firewall, no VMs.
Requires=strata-firewall.service
After=strata-firewall.service
EOF
done
systemctl daemon-reload
systemctl enable -q strata-firewall.service
if systemctl is-active -q strata-firewall.service; then
	nft -f "$CONF_DIR/firewall.nft"   # reload without restarting libvirt
else
	systemctl start strata-firewall.service
fi

step "2/9 Packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
	qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients virtinst \
	ovmf swtpm swtpm-tools libguestfs-tools python3-libvirt python3-guestfs \
	python3-venv websockify novnc xorriso cloud-image-utils
systemctl daemon-reload

step "3/9 Isolated VM network"
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/60-strata.conf
sysctl -q -p /etc/sysctl.d/60-strata.conf
if virsh -q net-info default >/dev/null 2>&1; then
	virsh -q net-destroy default >/dev/null 2>&1 || true
	virsh -q net-autostart default --disable
fi
for net in "strata $BRIDGE $SUBNET_PREFIX" "strata-build $BUILD_BRIDGE $BUILD_PREFIX"; do
	read -r name bridge prefix <<< "$net"
	render "$HERE/network.xml.in" "$CONF_DIR/network-$name.xml" "$name" "$bridge" "$prefix"
	virsh -q net-info "$name" >/dev/null 2>&1 || virsh -q net-define "$CONF_DIR/network-$name.xml"
	virsh -q net-autostart "$name"
	virsh -q net-info "$name" | grep -q '^Active:.*yes' || virsh -q net-start "$name"
done

step "4/9 Disk"
if [[ "$EXTEND_LVM" == yes ]]; then
	root_lv=$(findmnt -no SOURCE /)
	vg=$(lvs --noheadings -o vg_name "$root_lv" 2>/dev/null | tr -d ' ' || true)
	if [[ -n "$vg" ]]; then
		free=$(vgs --noheadings --units b --nosuffix -o vg_free "$vg" | tr -d ' ')
		if (( free > 1073741824 )); then
			lvextend -r -l +100%FREE "$root_lv"
		else
			echo "No free space left in $vg."
		fi
	else
		echo "/ is not on LVM, nothing to do."
	fi
fi
df -h /

step "5/9 Service account, config and storage"
id strata >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/strata \
	--no-create-home --shell /usr/sbin/nologin strata
getent group strata-admin >/dev/null || groupadd --system strata-admin
usermod -aG libvirt,kvm,strata-admin strata
if [[ ! -e "$CONF_DIR/strata.toml" ]]; then
	cat > "$CONF_DIR/strata.toml" <<EOF
# Strata settings. Restart the service after a change: systemctl restart stratad
public_host = "$LAN_IP"
port = 8080
subnet_prefix = "$SUBNET_PREFIX"
forward_ports = [${FORWARD_PORTS%-*}, ${FORWARD_PORTS#*-}]

[quota]   # default limits for a new agent
max_vms = 3
max_ram_mb = 16384
max_vcpus = 8
EOF
fi
chown root:strata "$CONF_DIR/strata.toml"
chmod 0640 "$CONF_DIR/strata.toml"
install -d -m 0711 "$DATA_DIR"
# Pools are setgid to the kvm group so every disk is owned :kvm and group-writable: qemu
# (libvirt-qemu:kvm) can read backing images and write overlays, and the strata service
# (also in kvm) can read frozen disks for offline diffs — without libvirt relabeling
# (disabled below), which cannot track Strata's out-of-band overlay/snapshot chains.
install -d -m 2775 -o root -g kvm "$DATA_DIR/images" "$DATA_DIR/vms" "$DATA_DIR/iso"
for pool in images vms iso; do
	name="strata-$pool"
	virsh -q pool-info "$name" >/dev/null 2>&1 || virsh -q pool-define-as "$name" dir --target "$DATA_DIR/$pool"
	virsh -q pool-autostart "$name"
	virsh -q pool-info "$name" | grep -q '^State:.*running' || virsh -q pool-start "$name"
done

# Deterministic disk ownership instead of libvirt's dynamic relabeling. Strata creates and
# re-chains overlays out of band (external snapshots, forks, reverts); libvirt's DAC/AppArmor
# label bookkeeping cannot follow that and ends up denying qemu access to backing files. With
# relabeling off and the setgid-kvm pools above, every disk is correctly owned from creation.
qconf=/etc/libvirt/qemu.conf
for kv in "dynamic_ownership = 0" "remember_owner = 0" "namespaces = [ ]"; do
	key=${kv%% *}
	if grep -qE "^\s*$key\s*=" "$qconf"; then
		sed -i "s|^\s*$key\s*=.*|$kv|" "$qconf"
	else
		echo "$kv" >> "$qconf"
	fi
done
# Apply the qemu.conf change to whichever qemu driver is in use (monolithic or modular).
systemctl try-restart libvirtd virtqemud 2>/dev/null || true

# Prebuilt libguestfs appliance for offline diffs. Built once as root here so the
# unprivileged service never needs to read the root-only host kernel in /boot.
APPLIANCE_DIR=/var/lib/strata/appliance
if [[ ! -f "$APPLIANCE_DIR/kernel" ]]; then
	install -d -m 0755 "$APPLIANCE_DIR"
	libguestfs-make-fixed-appliance "$APPLIANCE_DIR"
fi
# The service runs as the strata user and traverses its own 0700 state dir to reach the
# appliance; only the appliance dir and files need to be readable (the copied kernel is 0600).
chmod 0755 "$APPLIANCE_DIR"
chmod 0644 "$APPLIANCE_DIR"/kernel "$APPLIANCE_DIR"/initrd "$APPLIANCE_DIR"/root 2>/dev/null || true

step "6/9 Firewall helper"
install -d -m 0755 /usr/local/lib/strata
install -m 0755 -o root -g root "$REPO/helpers/strata-netctl" /usr/local/lib/strata/strata-netctl
cat > /etc/systemd/system/strata-netctl.socket <<'EOF'
[Unit]
Description=Strata firewall control socket

[Socket]
ListenStream=/run/strata-netctl.sock
SocketUser=root
SocketGroup=strata
SocketMode=0660
Accept=yes
MaxConnections=4

[Install]
WantedBy=sockets.target
EOF
cat > /etc/systemd/system/strata-netctl@.service <<EOF
[Unit]
Description=Strata firewall control
Requires=strata-firewall.service
After=strata-firewall.service

[Service]
ExecStart=/usr/bin/python3 -I /usr/local/lib/strata/strata-netctl --subnet $SUBNET_PREFIX.0/24 --ports $FORWARD_PORTS
StandardInput=socket
StandardOutput=socket
StandardError=journal
RuntimeMaxSec=30
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
SystemCallArchitectures=native
EOF
systemctl daemon-reload
systemctl enable -q --now strata-netctl.socket

step "7/9 Strata service"
install -d -m 0755 "$APP_DIR"
cat > /etc/systemd/system/stratad.service <<'EOF'
[Unit]
Description=Strata
After=network-online.target libvirtd.service strata-netctl.socket
Wants=network-online.target
Requires=strata-netctl.socket
ConditionPathExists=/opt/strata/.venv/bin/stratad

[Service]
User=strata
Group=strata
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/strata/.venv/bin/stratad
Restart=on-failure
RestartSec=5
StateDirectory=strata
StateDirectoryMode=0700
RuntimeDirectory=strata
RuntimeDirectoryMode=0755
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q stratad.service

step "8/9 Build-time access"
if [[ -n "$DEV_USER" && "$DEV_USER" != root ]]; then
	usermod -aG libvirt,strata-admin "$DEV_USER"
	chown "$DEV_USER:$DEV_USER" "$APP_DIR"
	cat > /etc/polkit-1/rules.d/50-strata-build.rules <<EOF
// Build-time only: lets $DEV_USER restart the Strata service. Removed by setup/lockdown.sh.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "stratad.service" &&
        subject.user == "$DEV_USER") {
        return polkit.Result.YES;
    }
});
EOF
	echo "$DEV_USER: libvirt and strata-admin groups, owns $APP_DIR, may restart stratad (until lockdown)."
fi

step "9/9 Checks"
fails=0
check() {
	local label=$1; shift
	if "$@" >/dev/null 2>&1; then printf '  [OK]   %s\n' "$label"
	else printf '  [FAIL] %s\n' "$label"; fails=$((fails + 1)); fi
}
daemon=libvirtd
systemctl is-active -q virtqemud 2>/dev/null && daemon=virtqemud
check "Strata firewall loaded" nft list table inet strata
check "$daemon requires the firewall" bash -c "systemctl show -p Requires $daemon.service | grep -q strata-firewall"
check "network 'strata' active" bash -c "virsh -q net-info strata | grep -q '^Active:.*yes'"
check "network 'strata-build' active" bash -c "virsh -q net-info strata-build | grep -q '^Active:.*yes'"
check "network 'default' stopped" bash -c "! virsh -q net-info default 2>/dev/null | grep -q '^Active:.*yes'"
check "firewall helper listening" systemctl is-active -q strata-netctl.socket
check "AppArmor enabled" aa-enabled -q
check "libvirt confines QEMU with AppArmor" bash -c "virsh -q capabilities | grep -q '<model>apparmor</model>'"
check "QEMU seccomp not disabled" bash -c "! grep -Eq '^\s*seccomp_sandbox\s*=\s*0' /etc/libvirt/qemu.conf"
check "VNC console bound to the host only" bash -c "! grep -Eq '^\s*vnc_listen\s*=\s*\"0\.0\.0\.0\"' /etc/libvirt/qemu.conf"
check "automatic security updates" systemctl is-enabled -q unattended-upgrades
echo
if (( fails == 0 )); then echo "Host ready."; else echo "$fails check(s) failed."; exit 1; fi

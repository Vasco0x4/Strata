#!/usr/bin/env bash
# Prepares a Linux host for Strata. Run once with sudo; safe to run again.
#
# What it does:
#   1. installs the Strata firewall and makes libvirt depend on it (no firewall, no VMs)
#   2. installs QEMU/KVM, libvirt, OVMF, swtpm, libguestfs
#   3. creates the isolated VM network and stops libvirt's "default" network
#   4. grows the root LVM volume into free space (EXTEND_LVM=no to skip)
#   5. creates the "strata" service account and storage pools
#   6. gives libvirt access to the user building Strata (removed at lockdown)
#   7. checks everything
#
# Settings (environment variables): LAN_IF, BRIDGE, SUBNET_PREFIX, DEV_USER, EXTEND_LVM
set -euo pipefail

LAN_IF="${LAN_IF:-$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')}"
BRIDGE="${BRIDGE:-strata0}"
SUBNET_PREFIX="${SUBNET_PREFIX:-10.99.0}"
DEV_USER="${DEV_USER:-${SUDO_USER:-}}"
EXTEND_LVM="${EXTEND_LVM:-yes}"

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF_DIR=/etc/strata
DATA_DIR=/srv/strata

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run it with sudo"
[[ -n "$LAN_IF" ]] && ip link show "$LAN_IF" >/dev/null 2>&1 || die "LAN interface not found (LAN_IF=$LAN_IF)"
[[ -e /dev/kvm ]] || die "/dev/kvm missing: no (nested) virtualization available"

render() {  # render <template> <destination>
	sed -e "s|@LAN_IF@|$LAN_IF|g" -e "s|@BRIDGE@|$BRIDGE|g" \
	    -e "s|@SUBNET@|$SUBNET_PREFIX.0/24|g" -e "s|@GATEWAY@|$SUBNET_PREFIX.1|g" \
	    -e "s|@DHCP_START@|$SUBNET_PREFIX.100|g" -e "s|@DHCP_END@|$SUBNET_PREFIX.250|g" \
	    "$1" > "$2"
}

step "1/7 Firewall"
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

step "2/7 Packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
	qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients virtinst \
	ovmf swtpm swtpm-tools libguestfs-tools python3-libvirt python3-guestfs \
	python3-venv websockify novnc xorriso cloud-image-utils
systemctl daemon-reload

step "3/7 Isolated VM network"
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/60-strata.conf
sysctl -q -p /etc/sysctl.d/60-strata.conf
if virsh -q net-info default >/dev/null 2>&1; then
	virsh -q net-destroy default >/dev/null 2>&1 || true
	virsh -q net-autostart default --disable
fi
render "$HERE/network.xml.in" "$CONF_DIR/network.xml"
virsh -q net-info strata >/dev/null 2>&1 || virsh -q net-define "$CONF_DIR/network.xml"
virsh -q net-autostart strata
virsh -q net-info strata | grep -q '^Active:.*yes' || virsh -q net-start strata

step "4/7 Disk"
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

step "5/7 Service account and storage"
id strata >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/strata \
	--create-home --shell /usr/sbin/nologin strata
usermod -aG libvirt,kvm strata
install -d -m 0711 "$DATA_DIR" "$DATA_DIR/images" "$DATA_DIR/vms"
for pool in images vms; do
	name="strata-$pool"
	virsh -q pool-info "$name" >/dev/null 2>&1 || virsh -q pool-define-as "$name" dir --target "$DATA_DIR/$pool"
	virsh -q pool-autostart "$name"
	virsh -q pool-info "$name" | grep -q '^State:.*running' || virsh -q pool-start "$name"
done

step "6/7 Build-time access"
if [[ -n "$DEV_USER" && "$DEV_USER" != root ]]; then
	usermod -aG libvirt "$DEV_USER"
	echo "$DEV_USER added to the libvirt group (removed at lockdown)."
fi

step "7/7 Checks"
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
check "network 'default' stopped" bash -c "! virsh -q net-info default 2>/dev/null | grep -q '^Active:.*yes'"
check "AppArmor enabled" aa-enabled -q
check "libvirt confines QEMU with AppArmor" bash -c "virsh -q capabilities | grep -q '<model>apparmor</model>'"
check "QEMU seccomp not disabled" bash -c "! grep -Eq '^\s*seccomp_sandbox\s*=\s*0' /etc/libvirt/qemu.conf"
check "VNC console bound to the host only" bash -c "! grep -Eq '^\s*vnc_listen\s*=\s*\"0\.0\.0\.0\"' /etc/libvirt/qemu.conf"
check "automatic security updates" systemctl is-enabled -q unattended-upgrades
echo
if (( fails == 0 )); then echo "Host ready."; else echo "$fails check(s) failed."; exit 1; fi

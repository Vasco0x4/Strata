#!/usr/bin/env bash
# Build an Ubuntu Strata image from the official cloud image (signature + checksum verified).
# Usage: build-ubuntu.sh [RELEASE]   e.g. build-ubuntu.sh 24.04     (default: 24.04)
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

RELEASE="${1:-24.04}"
DISK_GB="${DISK_GB:-20}"
NAME="ubuntu-$RELEASE"
VOLUME="$NAME.qcow2"
BASE="https://cloud-images.ubuntu.com/releases/$RELEASE/release"
KEYRING=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg

need curl qemu-img virt-install cloud-localds gpgv
work="$ISO_DIR/ubuntu-$RELEASE"
mkdir -p "$work"

log "fetching image list and signatures"
fetch "$BASE/SHA256SUMS" "$work/SHA256SUMS"
fetch "$BASE/SHA256SUMS.gpg" "$work/SHA256SUMS.gpg"
gpgv --keyring "$KEYRING" "$work/SHA256SUMS.gpg" "$work/SHA256SUMS" 2>/dev/null \
	|| die "signature check failed on SHA256SUMS (wrong keyring or tampered file)"
log "SHA256SUMS signature OK (Ubuntu cloud image keyring)"

img="noble"
img_file=$(grep -oE '[a-z]+-server-cloudimg-amd64\.img' "$work/SHA256SUMS" | head -n1)
[[ -n "$img_file" ]] || die "could not find a cloud image name for $RELEASE"
fetch "$BASE/$img_file" "$work/$img_file"
verify_sha256 "$work/$img_file" "$(grep " \*\?$img_file\$" "$work/SHA256SUMS" | cut -d' ' -f1)"

log "preparing disk ($DISK_GB GB)"
tmp="$work/build.qcow2"
rm -f "$tmp"
qemu-img create -f qcow2 -F qcow2 -b "$work/$img_file" "$tmp" >/dev/null
qemu-img resize "$tmp" "${DISK_GB}G" >/dev/null

# cloud-init: a "strata" user (password set per-VM via the guest agent), guest agent,
# SSH with password auth, then power off so the disk becomes a clean backing image.
cat > "$work/user-data" <<'CLOUD'
#cloud-config
users:
  - name: strata
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
ssh_pwauth: true
package_update: true
packages: [qemu-guest-agent, openssh-server]
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable --now ssh
power_state: {mode: poweroff, timeout: 300, condition: true}
CLOUD
echo "instance-id: strata-build" > "$work/meta-data"
cloud-localds "$work/seed.iso" "$work/user-data" "$work/meta-data"

dom="strata-build-$NAME"
cleanup_domain "$dom"
trap 'cleanup_domain "$dom"' EXIT
log "first boot (cloud-init runs, then powers off)"
virt-install --connect qemu:///system --name "$dom" --memory 2048 --vcpus 2 \
	--disk "$tmp",format=qcow2,bus=virtio --disk "$work/seed.iso",device=cdrom \
	--network network="$BUILD_NET",model=virtio --os-variant ubuntu"$RELEASE" \
	--graphics none --noautoconsole --import --channel unix,target_type=virtio,name=org.qemu.guest_agent.0

wait_shutoff "$dom" 900
cleanup_domain "$dom"
trap - EXIT

log "finalizing image"
qemu-img convert -O qcow2 -c "$tmp" "$IMAGES_DIR/$VOLUME"
rm -f "$tmp" "$work/seed.iso"
$VIRSH pool-refresh strata-images >/dev/null 2>&1 || true
register "$NAME" linux "$VOLUME" strata "Ubuntu $RELEASE (cloud image)"
log "done: $NAME"

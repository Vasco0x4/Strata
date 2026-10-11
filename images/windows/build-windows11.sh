#!/usr/bin/env bash
# Build a Windows 11 Strata image from the official Microsoft Enterprise evaluation ISO.
# Unattended install: real TPM 2.0 + Secure Boot, virtio drivers, local "strata" account,
# guest agent, SSH and RDP. The eval expires after ~90 days; rerun to rebuild.
#
#   build-windows11.sh [--iso /path/to/Windows11.iso]
# Without --iso, the current English Enterprise eval ISO is downloaded from Microsoft.
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../common.sh"

NAME="win11"
VOLUME="win11.qcow2"
DISK_GB="${DISK_GB:-64}"
RAM_MB="${RAM_MB:-8192}"
# Official Microsoft eval redirect (English, US), and the stable virtio-win ISO.
WIN_ISO_URL="${WIN_ISO_URL:-https://go.microsoft.com/fwlink/?LinkId=2382600&clcid=0x409&culture=en-us&country=us}"
VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
VIRTIO_SUM_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso-CHECKSUM"

need curl qemu-img virt-install xorriso
work="$ISO_DIR/win11"
mkdir -p "$work"

win_iso=""
while [[ $# -gt 0 ]]; do case "$1" in --iso) win_iso="$2"; shift 2;; *) die "unknown option $1";; esac; done

if [[ -z "$win_iso" ]]; then
	fetch "$WIN_ISO_URL" "$work/windows11-eval.iso"
	win_iso="$work/windows11-eval.iso"
	log "NOTE: Microsoft does not publish a checksum for the eval ISO; verifying size only."
	sz=$(stat -c%s "$win_iso")
	(( sz > 4000000000 )) || die "downloaded Windows ISO looks too small ($sz bytes)"
else
	[[ -f "$win_iso" ]] || die "ISO not found: $win_iso"
fi

log "fetching virtio-win driver ISO"
fetch "$VIRTIO_URL" "$work/virtio-win.iso"
fetch "$VIRTIO_SUM_URL" "$work/virtio-win.iso-CHECKSUM"
# The CHECKSUM file lists several algorithms; take the SHA256 line for this ISO.
sum=$(grep -iA3 'SHA256' "$work/virtio-win.iso-CHECKSUM" | grep -oiE '[0-9a-f]{64}' | head -n1)
[[ -n "$sum" ]] && verify_sha256 "$work/virtio-win.iso" "$sum" || log "no SHA256 in CHECKSUM, skipping virtio check"

log "building the unattended answer media"
answer="$work/answer"
rm -rf "$answer"; mkdir -p "$answer"
cp "$HERE/autounattend.xml" "$HERE/provision.ps1" "$answer/"
# A small ISO Setup will scan for autounattend.xml, plus the virtio drivers it references.
xorriso -as mkisofs -quiet -J -r -V STRATA -o "$work/answer.iso" "$answer"

disk="$IMAGES_DIR/$VOLUME"
rm -f "$disk"
qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null

dom="strata-build-win11"
cleanup_domain "$dom"
trap 'cleanup_domain "$dom"' EXIT
log "installing Windows (unattended; this takes several minutes)"
# E: must be the virtio ISO (autounattend.xml and provision.ps1 reference E:\...).
# CD order defines drive letters, so attach virtio before the answer media.
virt-install --connect qemu:///system --name "$dom" --memory "$RAM_MB" --vcpus 4 \
	--cpu host-passthrough --machine q35 \
	--boot uefi,loader.secure=yes --features smm.state=on --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
	--disk "$disk",format=qcow2,bus=virtio,boot.order=1 \
	--disk "$win_iso",device=cdrom,boot.order=2 \
	--disk "$work/virtio-win.iso",device=cdrom \
	--disk "$work/answer.iso",device=cdrom \
	--network network="$BUILD_NET",model=virtio \
	--osinfo "name=win11,require=off" --graphics vnc,listen=socket --noautoconsole \
	--channel unix,target_type=virtio,name=org.qemu.guest_agent.0

log "waiting for the unattended install to finish and power off (up to 60 min)"
wait_shutoff "$dom" 3600
cleanup_domain "$dom"
trap - EXIT

# Shrink the image now that install is done
log "finalizing image"
qemu-img convert -O qcow2 -c "$disk" "$disk.tmp" && mv "$disk.tmp" "$disk"
$VIRSH pool-refresh strata-images >/dev/null 2>&1 || true
register "$NAME" windows "$VOLUME" strata "Windows 11 Enterprise (evaluation)"
log "done: $NAME  (temporary setup password was StrataSetup!1; Strata resets it per VM)"

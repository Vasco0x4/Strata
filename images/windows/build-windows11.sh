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
# The stable-virtio link redirects to a versioned dir where the per-file CHECKSUM 404s, so we
# validate the ISO by size instead: a real virtio-win ISO is ~800 MB, an error/anti-bot page
# is a few KB. It comes from the official fedorapeople HTTPS host.
expect_min_size "$work/virtio-win.iso" 100000000

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
log "installing Windows (unattended; this takes a while)"
# provision.ps1 lives on the STRATA answer ISO and autounattend searches every drive for it
# (CD letters shift after the install reboots). virtio drivers are referenced by path too.
# Boot the DISK first. On the very first boot it is empty, so the firmware falls through to the
# Windows ISO (order 2) and the key-spam below answers its "Press any key" prompt to start Setup.
# On every later reboot during the install the disk has a bootloader and boots the installed
# Windows directly — so Setup is never re-entered from the CD (which aborts with "the computer
# restarted unexpectedly").
# Build-time performance: this is a throwaway build VM, so use cache=unsafe (skips the guest's
# fsyncs — a big speedup for Windows setup's many small writes; fine because a crash just means
# rebuild) and more vCPUs. Nested virtualization is inherently slow, especially with Windows 11's
# VBS; these cut the wall time noticeably. Tune with BUILD_VCPUS.
BUILD_VCPUS="${BUILD_VCPUS:-8}"
virt-install --connect qemu:///system --name "$dom" --memory "$RAM_MB" --vcpus "$BUILD_VCPUS" \
	--cpu host-passthrough --machine q35 \
	--boot uefi,loader.secure=yes --features smm.state=on --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
	--disk "$disk",format=qcow2,bus=virtio,boot.order=1,cache=unsafe,discard=unmap \
	--disk "$win_iso",device=cdrom,boot.order=2 \
	--disk "$work/virtio-win.iso",device=cdrom \
	--disk "$work/answer.iso",device=cdrom \
	--network network="$BUILD_NET",model=virtio \
	--osinfo "name=win11,require=off" --graphics vnc,listen=socket --noautoconsole \
	--channel unix,target_type=virtio,name=org.qemu.guest_agent.0

# The Windows ISO shows "Press any key to boot from CD or DVD" for only a few seconds. Spam
# Enter for the first ~90 s to get past it into Setup, then stop so later reboots fall through
# to the installed disk. Runs in the background while we wait for the install to finish.
( for _ in $(seq 1 90); do
	$VIRSH send-key "$dom" --codeset linux KEY_ENTER >/dev/null 2>&1 || true
	sleep 1
  done ) &

# Storage watchdog. On a thin-provisioned host we cannot see the backing store fill up from
# inside this VM (an exhausted Proxmox datastore shows as plenty-free here, then I/O fails and
# the host crashes). So bound how much the install may write: if the disk grows past
# MAX_BUILD_GB, force-stop the build instead of risking the host. Raise it when space is ample.
MAX_BUILD_GB="${MAX_BUILD_GB:-20}"
abort_flag="$work/.storage-abort"; rm -f "$abort_flag"
( while $VIRSH domstate "$dom" 2>/dev/null | grep -q running; do
	used_mb=$(du -m "$disk" 2>/dev/null | cut -f1)
	if [[ -n "$used_mb" ]] && (( used_mb > MAX_BUILD_GB * 1024 )); then
		touch "$abort_flag"; $VIRSH destroy "$dom" >/dev/null 2>&1; break
	fi
	sleep 20
  done ) &

# From here, leave the VM in place on failure so it can be inspected (screenshot, console)
# instead of being destroyed by the EXIT trap.
trap - EXIT
log "waiting for the unattended install to finish and power off (up to 90 min)"
if ! wait_shutoff "$dom" 5400; then
	[[ -f "$abort_flag" ]] && die "storage watchdog stopped the build: the disk exceeded ${MAX_BUILD_GB} GB.
  Free space (or raise MAX_BUILD_GB) and rerun."
	die "$dom did not power off in time. It was left running for inspection:
  sudo virsh -c qemu:///system screenshot $dom /tmp/win.ppm
  sudo virsh -c qemu:///system start $dom   (if already off)"
fi
[[ -f "$abort_flag" ]] && die "storage watchdog stopped the build: the disk exceeded ${MAX_BUILD_GB} GB."
cleanup_domain "$dom"
trap - EXIT

# The installed disk is used as the image as-is. We deliberately skip the usual
# `qemu-img convert -c` recompression: it writes a second full copy alongside the original
# (a transient storage spike), which on a tight thin-provisioned host can exhaust the
# backing store. Set WIN_COMPRESS=1 to recompress anyway when there is ample free space.
if [[ "${WIN_COMPRESS:-0}" == 1 ]]; then
	log "finalizing image (recompressing)"
	qemu-img convert -O qcow2 -c "$disk" "$disk.tmp" && mv "$disk.tmp" "$disk"
else
	log "finalizing image (no recompression; set WIN_COMPRESS=1 to shrink when space allows)"
fi
$VIRSH pool-refresh strata-images >/dev/null 2>&1 || true
register "$NAME" windows "$VOLUME" strata "Windows 11 Enterprise (evaluation)"
log "done: $NAME  (temporary setup password was StrataSetup!1; Strata resets it per VM)"

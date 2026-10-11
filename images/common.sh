# Shared helpers for image builders. Source this; do not run it.
# Every builder: downloads official media (checksum/signature verified), builds on the
# isolated strata-build network, installs the guest agent, then registers the result as
# a Strata image. Builders run as a user in the libvirt group; no root needed.
set -euo pipefail

STRATA_DATA="${STRATA_DATA:-/srv/strata}"
ISO_DIR="$STRATA_DATA/iso"
IMAGES_DIR="$STRATA_DATA/images"
BUILD_NET="${STRATA_BUILD_NET:-strata-build}"
VIRSH="virsh -c qemu:///system"

log() { printf '\033[1m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

need() { for c in "$@"; do command -v "$c" >/dev/null || die "missing command: $c"; done; }

# fetch URL DEST -- download only if absent, follow redirects, fail on HTTP errors.
# Resumable (-C -) with generous retries: large vendor downloads (the ~8 GB Windows ISO)
# drop mid-transfer, and without resume curl would restart from zero and may never finish.
fetch() {
	local url=$1 dest=$2
	[[ -s "$dest" ]] && { log "have $(basename "$dest")"; return; }
	log "downloading $(basename "$dest")"
	# No browser User-Agent on purpose: fedorapeople.org's anti-bot challenges browser-like
	# UAs (serving an HTML page instead of the file) but allows the default curl UA, and
	# Microsoft's download works with it too.
	curl -fL -C - --retry 10 --retry-delay 5 --retry-all-errors \
		--connect-timeout 30 -o "$dest.part" "$url"
	mv "$dest.part" "$dest"
}

# expect_min_size FILE BYTES -- guard against anti-bot HTML or truncated downloads
expect_min_size() {
	local file=$1 min=$2 size
	size=$(stat -c%s "$file" 2>/dev/null || echo 0)
	(( size >= min )) || die "$(basename "$file") is only $size bytes (expected >= $min);
  the server may have returned an error or anti-bot page instead of the file."
}

# verify_sha256 FILE EXPECTED
verify_sha256() {
	local file=$1 expected=$2 got
	got=$(sha256sum "$file" | cut -d' ' -f1)
	[[ "$got" == "$expected" ]] || die "checksum mismatch for $(basename "$file")
  expected $expected
  got      $got"
	log "checksum OK: $(basename "$file")"
}

# register NAME OS VOLUME USERNAME DESCRIPTION -- make the built image usable by Strata
register() {
	local name=$1 os=$2 volume=$3 username=$4 desc=$5
	if [[ -S /run/strata/admin.sock ]] && command -v strata >/dev/null; then
		strata register-image "$name" --os "$os" --volume "$volume" --username "$username" --description "$desc"
	else
		cat <<MSG

Image built: $IMAGES_DIR/$volume
Register it once the service is running, as a member of strata-admin:
  strata register-image $name --os $os --volume $volume --username $username --description "$desc"
MSG
	fi
}

# wait_shutoff DOMAIN TIMEOUT -- wait for an unattended install to power off by itself
wait_shutoff() {
	local dom=$1 timeout=$2 start
	start=$(date +%s)
	while $VIRSH domstate "$dom" 2>/dev/null | grep -q running; do
		(( $(date +%s) - start > timeout )) && return 1   # caller decides what to do
		sleep 15
	done
}

cleanup_domain() {
	$VIRSH destroy "$1" 2>/dev/null || true
	$VIRSH undefine "$1" --nvram --tpm 2>/dev/null || true
}

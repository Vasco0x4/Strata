#!/usr/bin/env bash
# Live acceptance test for Strata. Run AFTER install-host.sh, deploy.sh and at least one
# Linux image, as a member of strata-admin (uses the local admin socket). It creates real
# VMs, exercises the core actions, and — most importantly — proves a VM cannot reach the
# host, the LAN, the parent hypervisor or the internet unless explicitly allowed. It
# cleans up after itself. Nothing here needs editing; it encodes the "done when" criteria.
#
#   bash setup/smoke-test.sh [IMAGE]      (default image: the first Linux image registered)
set -uo pipefail

STRATA="${STRATA:-strata}"
VM1=smoke-$$-a
VM2=smoke-$$-b
LAN_PROBE="${LAN_PROBE:-10.50.0.1}"   # an address on your home/Proxmox LAN; must stay unreachable
fails=0

pass() { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fails=$((fails+1)); }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# Destroy the fork (VM2) before VM1: VM1 cannot be removed while a VM forked from its snapshot exists.
cleanup() { $STRATA destroy -y "$VM2" >/dev/null 2>&1 || true; $STRATA destroy -y "$VM1" >/dev/null 2>&1 || true; }
trap cleanup EXIT

command -v "$STRATA" >/dev/null || { echo "strata CLI not found; run setup/deploy.sh"; exit 1; }

IMAGE="${1:-$($STRATA --json images 2>/dev/null | python3 -c 'import sys,json
imgs=[i for i in json.load(sys.stdin) if i["os"]=="linux"]
print(imgs[0]["name"] if imgs else "")' )}"
[[ -n "$IMAGE" ]] || { echo "no Linux image registered; build one first (images/linux/build-ubuntu.sh)"; exit 1; }

# in_vm VM "command" -> prints guest stdout, returns guest exit code
in_vm() { $STRATA --json exec "$1" -- "$2" 2>/dev/null | python3 -c 'import sys,json
r=json.load(sys.stdin); sys.stdout.write(r.get("stdout","")); sys.exit(r.get("exit_code") or 0)'; }

# reachable VM HOST PORT -> 0 if a TCP connection completes within 4s, 1 if blocked/refused
reachable() {
  in_vm "$1" "timeout 4 bash -c 'echo > /dev/tcp/$2/$3' 2>/dev/null && echo OPEN || echo BLOCKED" | grep -q OPEN
}

step "Using image '$IMAGE'"

step "Create, exec, files"
$STRATA create "$IMAGE" --name "$VM1" >/dev/null && pass "created $VM1" || { fail "create"; exit 1; }
[[ "$(in_vm "$VM1" 'echo strata-ok')" == *strata-ok* ]] && pass "exec returns output" || fail "exec"
echo "payload-$$" > /tmp/strata-smoke.txt
$STRATA upload "$VM1" /tmp/strata-smoke.txt /tmp/up.txt >/dev/null && \
  [[ "$(in_vm "$VM1" 'cat /tmp/up.txt')" == "payload-$$" ]] && pass "upload" || fail "upload"
$STRATA download "$VM1" /tmp/up.txt /tmp/strata-dl.txt >/dev/null && \
  [[ "$(cat /tmp/strata-dl.txt)" == "payload-$$" ]] && pass "download round-trips" || fail "download"

step "Snapshot, diff, revert, fork"
$STRATA snapshot "$VM1" s1 --no-memory >/dev/null && pass "snapshot s1" || fail "snapshot"
in_vm "$VM1" 'echo marker > /root/marker.txt' >/dev/null
$STRATA snapshot "$VM1" s2 --no-memory >/dev/null && pass "snapshot s2" || fail "snapshot"
$STRATA --json diff "$VM1" s1 s2 | grep -q marker.txt && pass "diff shows the new file" || fail "diff"
$STRATA revert "$VM1" s1 >/dev/null 2>&1
in_vm "$VM1" 'test -f /root/marker.txt' && fail "revert did not undo the change" || pass "revert undoes the change"
$STRATA fork "$VM1" s1 --name "$VM2" >/dev/null && pass "fork from snapshot" || fail "fork"

step "Isolation (the core guarantee)"
reachable "$VM1" 10.99.0.1 22   && fail "VM can reach the host gateway" || pass "host unreachable"
reachable "$VM1" "$LAN_PROBE" 22 && fail "VM can reach the LAN ($LAN_PROBE)" || pass "LAN/Proxmox unreachable"
reachable "$VM1" 1.1.1.1 443    && fail "VM has internet by default" || pass "internet blocked by default"
reachable "$VM1" 10.99.0.1 67   && pass "note: DHCP path exists (expected)" || true  # informational

step "Opt-in internet, LAN still blocked"
$STRATA internet "$VM1" on >/dev/null
sleep 2
reachable "$VM1" 1.1.1.1 443    && pass "internet reachable once enabled" || fail "internet did not come on"
reachable "$VM1" "$LAN_PROBE" 22 && fail "LAN reachable with internet on" || pass "LAN still blocked with internet on"
$STRATA internet "$VM1" off >/dev/null

echo
if (( fails == 0 )); then echo -e "\033[32mAll checks passed.\033[0m"; else echo -e "\033[31m$fails check(s) failed.\033[0m"; exit 1; fi

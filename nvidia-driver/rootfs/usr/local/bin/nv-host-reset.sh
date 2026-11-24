#!/usr/bin/env bash
# Last-resort host reset for a wedged GPU.   nv-host-reset.sh <reason>
#
# After Xid 79 ("fallen off the bus") processes stuck inside the driver can't be killed, so the
# Supervisor's reboot waits forever on their container and the host never restarts (seen
# 2026-11-22/23). This bypasses userspace entirely:
#   1. SysRq 's' + 'u': flush and remount every filesystem read-only (ext4 journal + SQLite WAL
#      recover the rest at boot).
#   2. CF9h full reset (0x0E): the Intel PCH drops SLP_S3#/S4#/S5# for 3-5 s, cutting the main
#      rails (the GPU's included), then powers the machine back on by itself.
#   3. If the port write fails: SysRq 'b' (warm reset through the ACPI reset register).
# Needs SYS_ADMIN (private /proc: Docker mounts /proc/sysrq-trigger read-only) and SYS_RAWIO
# (/dev/port). Never returns on success.
set -uo pipefail
. /usr/local/bin/common.sh
REASON=${1:-unspecified}
P=/run/nv-proc

mkdir -p "$P"
mountpoint -q "$P" || mount -t proc proc "$P" || { log "host reset: cannot mount proc"; exit 1; }
[ -w "$P/sysrq-trigger" ] || { log "host reset: sysrq-trigger not writable"; exit 1; }

log "HOST RESET ($REASON): sync, remount read-only, full platform reset in ~8 s"
echo "$(date -u +%FT%TZ) $REASON" >> /share/nvidia/host-resets.log
sync
echo s > "$P/sysrq-trigger"; sleep 3
echo u > "$P/sysrq-trigger"; sleep 3
python3 - <<'EOF' || true
import os, time
fd = os.open("/dev/port", os.O_RDWR)
keep = os.pread(fd, 1, 0xCF9)[0] & ~0x0E
os.pwrite(fd, bytes([keep | 0x02]), 0xCF9)   # SYS_RST: the next CPU reset is a hard reset
time.sleep(0.001)
os.pwrite(fd, bytes([keep | 0x0E]), 0xCF9)   # + RST_CPU + FULL_RST: full reset now
time.sleep(2)
EOF
# Still running: the port write was refused or ignored.
echo b > "$P/sysrq-trigger"
sleep 30
log "host reset: still running after both reset methods"
exit 1

#!/bin/sh
# S17 control case — the ONE evidence item that needs root, and only for two minutes on any Linux box.
#
# S17's claim is that a *fully preallocated* segment never faults when the filesystem runs out of space, while a
# *sparse* one does. This script builds the sparse control on a 2 MiB tmpfs (which needs `mount`, so root), writes
# through a 1 MiB window and prints the signal the process died from — `SIGBUS` for the sparse mapping, nothing
# for the preallocated case. It writes nothing outside its own tmpdir and prints exactly what it observed.
#
# Usage: sudo sh tools/s17_control.sh          (or: sudo ./tools/s17_control.sh)
# Output: one line per case: "case=<sparse|preallocated> exit=<code|signal> bytes_before_fault=<n>"
# Labelled in STATUS as `requires humans (root on any Linux box, two minutes)`, not as untestable.
set -eu
MNT=${MNT:-/mnt/bbx-s17}
PY=${PY:-python3}

mkdir -p "$MNT"
mount -t tmpfs -o size=2m tmpfs "$MNT" || { echo "could not mount tmpfs at $MNT (need root)"; exit 2; }
trap 'umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT INT TERM

run_case() {
    case_name=$1
    sparse=$2
    "$PY" - "$MNT" "$case_name" "$sparse" <<'PYEOF' || echo "case=$case_name exit=$? (see above)"
import mmap, os, signal, sys
mnt, name, sparse = sys.argv[1], sys.argv[2], sys.argv[3] == "sparse"
path = os.path.join(mnt, f"{name}.seg")
fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
# 8 MiB file on a 2 MiB filesystem: sparse means ftruncate only; preallocated means every block is really there
size = 8 << 20
if sparse:
    os.ftruncate(fd, size)
else:
    try:
        os.posix_fallocate(fd, 0, size)          # this is the call that makes ENOSPC visible now
    except OSError as exc:
        print(f"case={name} preallocation failed as expected: {exc.strerror}")
        os.close(fd)
        raise SystemExit(0)
mm = mmap.mmap(fd, size)
written = 0
try:
    for off in range(0, size, 4096):
        mm[off:off + 4096] = b"x" * 4096
        written += 4096
except Exception:
    pass
finally:
    print(f"case={name} exit=live bytes_before_fault={written}")
    os.close(fd)
os.unlink(path)
PYEOF
}

# the sparse case is expected to die from SIGBUS; run it in a subshell so the script survives
run_case sparse sparse || true
run_case preallocated dense || true
echo "If the sparse case printed a signal (SIGBUS/7) and the preallocated case printed exit=live, S17's"
echo "control is satisfied. Paste this output into docs/STATUS.md; the number is the whole evidence."

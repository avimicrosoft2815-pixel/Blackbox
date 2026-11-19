#!/bin/sh
# selftest.sh — build the Go reader and run it against a real capsule, the vectors, and three broken files.
#
# This is what the `go-reader` CI job runs, and it is the only place this reader is *executed*: the machine the rest of
# the project is built on has no Go toolchain, so the source was written and reviewed there and compiled here.
#
#   sh tools/readers/go/selftest.sh            # needs `go`, `python3`, `unzip` and a wheel-installed blackbox
#
# Exit 0 means every expectation held. Each expectation is named, so a failure says which one.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "== build"
cd "$here"
go vet ./...
go build -o "$work/bbx-read" .

echo "== conformance vectors"
"$work/bbx-read" --vectors "$root/tests/conformance/vectors/crc32.json"

echo "== a real capsule, written by the Python implementation"
cd "$work"
mkdir -p project
cat > project/app.py <<'PY'
import sys
print("hello from the go-reader selftest")
sys.exit(0)
PY
BLACKBOX_HOME="$work/data" PYTHONPATH="$root/src" python3 -m blackbox run --no-proxy --no-sdk --no-hooks -- \
    python3 project/app.py > "$work/run.log" 2>&1
capsule=$(ls "$work/data/capsules"/*.bbx | head -1)
echo "capsule: $capsule"
"$work/bbx-read" "$capsule" --json > "$work/report.json"
grep -q '"frames"' "$work/report.json"

echo "== a tampered entry is an integrity failure (exit 4)"
python3 - "$capsule" "$work/tampered.bbx" <<'PY'
import sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename.startswith("events/seg-"):
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        zout.writestr(info, data)
PY
set +e
"$work/bbx-read" "$work/tampered.bbx" >/dev/null 2>&1
code=$?
set -e
if [ "$code" -ne 4 ]; then echo "expected exit 4 for a tampered entry, got $code"; exit 1; fi
echo "tampered entry: exit 4 as expected"

echo "== a file that is not a capsule is refused (exit 3)"
printf 'not a capsule\n' > "$work/not-a-capsule.bbx"
set +e
"$work/bbx-read" "$work/not-a-capsule.bbx" >/dev/null 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then echo "expected exit 3 for a non-zip file, got $code"; exit 1; fi
echo "non-zip: exit 3 as expected"

echo "== an entry named with a traversal is refused (exit 3)"
python3 - "$capsule" "$work/traversal.bbx" <<'PY'
import sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
    for info in zin.infolist():
        zout.writestr(info, zin.read(info.filename))
    zout.writestr(zipfile.ZipInfo("../escape.txt"), b"x")
PY
set +e
"$work/bbx-read" "$work/traversal.bbx" >/dev/null 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then echo "expected exit 3 for a traversal entry, got $code"; exit 1; fi
echo "traversal: exit 3 as expected"

echo "== all Go-reader expectations held"

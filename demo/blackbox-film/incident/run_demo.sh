#!/usr/bin/env bash
# Reproduce the demo incident from scratch: a synthetic app, a mock provider that cuts the stream, and BlackBox.
#
#   ./run_demo.sh [work-dir]        # default: /tmp/blackbox-demo-incident
#
# Nothing here touches the network: the "provider" is a local Python server and the app talks to it through the
# recorder's loopback proxy. Every id, order and token in the output is invented.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="${1:-/tmp/blackbox-demo-incident}"
port="${PORT:-8787}"
python="${PYTHON:-$(command -v python3)}"
blackbox="${BLACKBOX:-$(command -v blackbox || echo "$here/../../../.venv/bin/blackbox")}"

rm -rf "$work"; mkdir -p "$work"
"$python" "$here/mock_provider.py" "$port" > "$work/provider.log" 2>&1 &
provider=$!
trap 'kill $provider 2>/dev/null || true' EXIT
sleep 1

cd "$work"
"$blackbox" run --data-dir "$work/data" \
    --upstream "openai=http://127.0.0.1:$port@/openai" \
    --json -- "$python" "$here/app.py" > run.json 2> run.err || true

"$blackbox" ls --data data                     > ls.txt      2>&1 || true
"$blackbox" verify data/capsules/*.bbx         > verify.txt  2>&1 || true
"$blackbox" diagnose data --json               > diagnosis.json 2>/dev/null || true
"$blackbox" diagnose data --out report.md --brief brief.md > diagnose.log 2>&1 || true

echo "wrote $work/{run.json,ls.txt,verify.txt,diagnosis.json,report.md,brief.md}"

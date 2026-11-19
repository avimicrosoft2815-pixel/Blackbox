#!/bin/sh
# The Linux desktop journey, on a machine that has a screen.
#
# This is the one part of the release this repository cannot do for itself: a window has to be *seen*. Everything the
# script can check, it checks; everything only a person can see, it asks about and records.
#
#   sh tools/desktop_journey.sh                 # uses the standard data directory
#   sh tools/desktop_journey.sh --app 'python3 my_service.py'   # record your own app instead of the sample
#   sh tools/desktop_journey.sh --out ~/journey.json
#   sh tools/desktop_journey.sh --automated-only   # no display needed: records, lists, verifies, diagnoses, writes the file
#
# It stops at the first check it can decide for itself, so a machine with no display gets one clear sentence rather than
# a wall of failures. The answers go into a JSON file you can send back — that file is the evidence, and it is the only
# thing this script produces.
#
# What it does NOT do: claim anything. If you answer "no" to "the window opened", the file says so, and the platform
# matrix in docs/desktop.md stays "not run" until a run says otherwise.
set -eu

APP=""
OUT=""
DATA_DIR="${BLACKBOX_HOME:-}"
AUTOMATED_ONLY=no

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --data) DATA_DIR="$2"; shift 2 ;;
    --automated-only) AUTOMATED_ONLY=yes; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf '\n!! %s\n' "$*" >&2; exit 1; }

ask() {   # ask <question> [y to default yes]
  printf '%s ' "$1"
  if [ "${2:-}" = "y" ]; then printf '[Y/n] '; else printf '[y/N] '; fi
  read -r answer || answer=""
  answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')
  case "$answer" in
    y|yes) printf 'yes' ;;
    n|no)  printf 'no' ;;
    "")    if [ "${2:-}" = "y" ]; then printf 'yes'; else printf 'no'; fi ;;
    *)     printf 'no' ;;
  esac
}

note() { printf '%s: ' "$1"; read -r line || line=""; printf '%s' "$line"; }

# ── 1. the machine ───────────────────────────────────────────────────────────────────────────────────────────────
step "1/6 This machine"
say "  uname:   $(uname -sm)"
say "  display: DISPLAY=${DISPLAY:-<unset>} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<unset>}"
command -v blackbox >/dev/null 2>&1 || fail "blackbox is not on PATH. Install the .deb (docs/desktop.md) or pip install 'blackbox-recorder[desktop]'."
say "  version: $(blackbox --version 2>&1)"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  if [ "$AUTOMATED_ONLY" = "no" ]; then
    fail "no display. This is the desktop journey; run it from a graphical session (or 'ssh -X' with X forwarding),
     or run 'sh tools/desktop_journey.sh --automated-only' to record the half that needs no screen."
  fi
  say "  (no display: --automated-only, so the window steps are recorded as not attempted)"
else
  say "  (a graphical session is present)"
fi

# ── 2. the window stack ─────────────────────────────────────────────────────────────────────────────────────────
step "2/6 The window stack"
if [ "$AUTOMATED_ONLY" = "no" ]; then
  if ! blackbox desktop --selftest; then
    fail "the window cannot be opened here. Install what the line above names, e.g. on Debian/Ubuntu:
     sudo apt install -y python3-gi gir1.2-webkit2-4.1"
  fi
else
  blackbox desktop --selftest || say "  (that is why the window steps are skipped)"
fi

# ── 3. record something to look at ──────────────────────────────────────────────────────────────────────────────
step "3/6 Record an incident"
SAMPLE_DIR=$(mktemp -d)
cat > "$SAMPLE_DIR/app.py" <<'PY'
"""A program that fails on purpose: the journey needs an incident, not a happy path."""
import json
import sys

print(json.dumps({"step": "starting"}))
raise RuntimeError("the journey's deliberate failure: a handler that raises")


PY
if [ -n "$APP" ]; then
  say "  recording your app: $APP"
  run_command="$APP"
else
  say "  no --app given: recording a four-line program that raises on purpose"
  run_command="python3 $SAMPLE_DIR/app.py"
fi
say "  command: blackbox run -- $run_command"
set +e
sh -c "blackbox run -- $run_command" >"$SAMPLE_DIR/run.log" 2>&1
run_status=$?
set -e
say "  the recorded program exited ${run_status} (non-zero is expected for the sample)"
say "  capsules now:"
blackbox ls 2>&1 | sed 's/^/    /' || true

CAPSULE=$(blackbox ls --json 2>/dev/null | python3 -c 'import json,sys
rows = json.load(sys.stdin)
rows = rows.get("capsules", rows) if isinstance(rows, dict) else rows
print(rows[0]["capsule"] if rows else "")' 2>/dev/null || true)
[ -n "$CAPSULE" ] || say "  (could not pick a capsule automatically; the checks below will say so)"

# ── 4. the window ──────────────────────────────────────────────────────────────────────────────────────────────
step "4/6 The window — this part needs your eyes"
if [ "$AUTOMATED_ONLY" = "yes" ]; then
  say "  skipped (--automated-only): a window cannot be seen without a screen, and this file will say so"
  window_status="skipped"
  window_opened="not-attempted"; setup_seen="not-attempted"; run_opened="not-attempted"
  verify_ok="not-attempted"; diagnose_seen="not-attempted"; closed_clean="not-attempted"; window_note=""
else
cat <<'TEXT'
  Opening the app now. In the window:
    a. the first screen: either setup (nothing recorded yet) or "recorded runs"
    b. click a run → the timeline draws, with the action bar at the bottom
    c. click "Check this capsule" → a green "verified"
    d. click "Diagnose" → findings, then the repair brief as text
    e. close the window (title-bar X)

  Nothing is measured by this script while it is open; you are the instrument.
TEXT
printf '\n  press Enter to open the window... '
read -r _ || true
set +e
blackbox desktop
window_status=$?
set -e
say "  the window command exited ${window_status}"

window_opened=$(ask "  Did the window actually open (a title bar and the black-and-gold page)?")
setup_seen=$(ask "  Did you see the setup page or the run list, as described?" y)
run_opened=$(ask "  Did a run open as a timeline?")
verify_ok=$(ask "  Did 'Check this capsule' report verified?")
diagnose_seen=$(ask "  Did 'Diagnose' show findings and a brief?")
closed_clean=$(ask "  Did the window close without an error in the terminal?" y)
window_note=$(note "  Anything wrong with what you saw (one line, or empty)")
fi

# ── 5. the command line, for the same capsule ─────────────────────────────────────────────────────────────────
step "5/6 The same capsule from the command line"
if [ -n "$CAPSULE" ]; then
  set +e
  blackbox verify "$CAPSULE" >"$SAMPLE_DIR/verify.log" 2>&1; verify_status=$?
  blackbox diagnose "$CAPSULE" --out "$SAMPLE_DIR/report.md" >"$SAMPLE_DIR/diagnose.log" 2>&1; diagnose_status=$?
  set -e
  say "  verify exit ${verify_status}, diagnose exit ${diagnose_status}; report at $SAMPLE_DIR/report.md"
else
  verify_status="skipped"
  diagnose_status="skipped"
  say "  skipped: no capsule was found by 'blackbox ls --json'"
fi

# ── 6. the evidence file ──────────────────────────────────────────────────────────────────────────────────────
step "6/6 Writing the journey record"
if [ -z "$OUT" ]; then
  OUT="$PWD/desktop-journey-$(date -u +%Y%m%dT%H%M%SZ).json"
fi
python3 - "$OUT" <<PY
import json, platform, subprocess, sys, datetime

out = sys.argv[1]
record = {
    "schema": "blackbox.desktop-journey/1",
    "when": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "machine": {"system": platform.system(), "machine": platform.machine(),
                "distro": platform.freedesktop_os_release().get("PRETTY_NAME", "") if hasattr(platform, "freedesktop_os_release") else "",
                "python": platform.python_version()},
    "data_dir": "$DATA_DIR" or None,
    "recorded_command": """$run_command""",
    "capsule": "$CAPSULE" or None,
    "automated": {"run_exit": $run_status, "window_exit": "$window_status",
                  "verify_exit": "$verify_status", "diagnose_exit": "$diagnose_status"},
    "seen_by_a_person": {
        "window_opened": "$window_opened",
        "first_screen": "$setup_seen",
        "run_opened": "$run_opened",
        "check_verified": "$verify_ok",
        "diagnose_shown": "$diagnose_seen",
        "closed_cleanly": "$closed_clean",
        "note": """$window_note""",
    },
    "verdict": ("automated-only" if "$window_opened" == "not-attempted" else
                "pass" if all(x == "yes" for x in ("$window_opened", "$run_opened", "$verify_ok", "$diagnose_seen"))
                else "incomplete"),
    "what_this_is": ("One person's answers plus the machine's own exit codes, on one Linux desktop. It is evidence for "
                     "the row in docs/desktop.md that names this distribution and architecture, and for nothing else."),
}
with open(out, "w", encoding="utf-8", newline="\\n") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\\n")
print(f"  wrote {out}")
print(f"  verdict: {record['verdict']}")
PY
say ""
say "Send that file back, or paste its 'seen_by_a_person' block into docs/desktop.md's platform matrix with the"
say "distribution and architecture named. If any answer above was 'no', the row stays 'not run' — that is the point."

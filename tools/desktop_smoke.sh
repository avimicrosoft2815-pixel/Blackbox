#!/bin/sh
# What a person on a real desktop runs, once, to turn "the code path is tested" into "the window appeared here".
#
#   sh tools/desktop_smoke.sh [capsule.bbx|data-dir]
#
# It prints one line per check and exits non-zero on the first failure, so the output can be pasted into
# `docs/desktop.md`'s platform matrix without interpretation. Run it on the machine you intend to support, with a
# display attached; over SSH or in a container without one, it will stop at check 2 and say why.
set -u
fail=0
say() { printf '%s\n' "$*"; }
ok() { say "ok   $*"; }
bad() { say "FAIL $*"; fail=1; }

say "== 1. the binary and the build line"
blackbox --version || bad "blackbox --version"
[ $fail -eq 0 ] && ok "cli runs"

say "== 2. the window stack on this machine"
selftest=$(blackbox desktop --selftest 2>&1); code=$?
say "$selftest"
if [ $code -eq 0 ]; then ok "window stack present"; else bad "window stack missing (see the install line above)"; exit 1; fi

say "== 3. a window opens, serves the page and closes when the window is closed"
target=${1:-}
if [ -z "$target" ]; then
  target=$(blackbox ls --json 2>/dev/null | sed -n 's/.*"data_dir": *"\([^"]*\)".*/\1/p' | head -1)
fi
if [ -z "$target" ]; then
  bad "no capsule or data directory found; run a recording first, or pass a path"
  exit 1
fi
say "     target: $target"
start=$(date +%s)
blackbox desktop "$target" & pid=$!
sleep 4
if kill -0 "$pid" 2>/dev/null; then
  ok "the app is running with a window (pid $pid) — close the window to continue"
  wait "$pid"; code=$?
  [ $code -eq 0 ] && ok "closed cleanly after $(( $(date +%s) - start ))s" || bad "exit $code on close"
else
  wait "$pid"; code=$?
  bad "the app exited on its own with $code (a window that never appeared, or one that crashed)"
fi

if [ $fail -eq 0 ]; then
  say "== all checks passed on $(uname -s) $(uname -m), $(date -u +%Y-%m-%dT%H:%MZ)"
  say "   copy this line into docs/desktop.md's platform matrix"
else
  say "== something failed above; the matrix stays 'not run' until it does not"
fi
exit $fail

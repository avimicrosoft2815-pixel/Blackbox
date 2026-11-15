"""Report CI for a branch, and fail when the newest completed run for HEAD is not success (C35).

The defect this exists for is a reporting one: STATUS cited *"green, run 43660321110 (commit a9f0953, the PR head)"*
while seven consecutive runs on the live branch were red. A verdict is about a commit, so the tool prints the sha of
every run's `head_sha`, decides against **HEAD**, and is meant to be run before STATUS is written and before every
`STOPPED:` line.

No token, no login: the repository is public, so this is a plain GET of the runs JSON.

    python tools/ci_status.py                       # the current branch
    python tools/ci_status.py arena/a781c68f-blackbox
    python tools/ci_status.py --json                # machine-readable, same verdict

Exit codes: 0 = the newest completed run for HEAD is `success`; 1 = it is not (or there is none, which is reported as
`pending for HEAD`); 2 = the query itself failed (network, rate limit), which is **not** evidence of green.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "avimicrosoft2815-pixel/Blackbox"
API = f"https://api.github.com/repos/{REPO}/actions/runs"
PER_PAGE = 20


def current_branch() -> str:
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return proc.stdout.strip() or "HEAD"


def head_sha() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return proc.stdout.strip()


def fetch_runs(branch: str, *, per_page: int = PER_PAGE) -> list[dict]:
    url = f"{API}?branch={urllib.parse.quote(branch, safe='')}&per_page={per_page}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "blackbox-ci-status"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return list(payload.get("workflow_runs", []))


def summarise(runs: list[dict], *, head: str) -> dict:
    """The verdict is about HEAD, and about the newest *completed* run that has HEAD as its sha."""
    completed = [r for r in runs if r.get("status") == "completed"]
    mine = [r for r in completed if str(r.get("head_sha", "")).startswith(head[:7]) or head.startswith(
        str(r.get("head_sha", ""))[:7])]
    newest = max(mine, key=lambda r: r.get("created_at", ""), default=None)
    latest_any = max(completed, key=lambda r: r.get("created_at", ""), default=None)
    return {
        "head": head,
        "runs": [{"id": r.get("id"), "sha": r.get("head_sha"), "conclusion": r.get("conclusion"),
                  "url": r.get("html_url"), "status": r.get("status"),
                  "created_at": r.get("created_at")} for r in
                 sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)],
        "head_run": newest and {"id": newest.get("id"), "sha": newest.get("head_sha"),
                                "conclusion": newest.get("conclusion"), "url": newest.get("html_url")},
        "last_completed": latest_any and {"id": latest_any.get("id"), "sha": latest_any.get("head_sha"),
                                          "conclusion": latest_any.get("conclusion"),
                                          "url": latest_any.get("html_url")},
        "verdict": ("green" if newest and newest.get("conclusion") == "success"
                    else "pending for HEAD" if newest is None else f"RED ({newest.get('conclusion')})"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI verdict for a branch's HEAD")
    ap.add_argument("branch", nargs="?", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    branch = args.branch or current_branch()
    head = head_sha()
    try:
        runs = fetch_runs(branch)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"ci-status: query failed for {branch}: {exc}", file=sys.stderr)
        print("ci-status: a failed query is not evidence of green", file=sys.stderr)
        return 2
    for run in sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True):
        print(f"{(run.get('head_sha') or '')[:7]} · {run.get('conclusion') or run.get('status')} · "
              f"{run.get('html_url')}")
    info = summarise(runs, head=head)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0 if info["verdict"] == "green" else 1
    if info["verdict"] == "green":
        print(f"ci-status: {branch} @ {head[:7]} is green (run {info['head_run']['id']})")
        return 0
    if info["verdict"] == "pending for HEAD":
        last = info["last_completed"]
        tail = (f"last completed: run {last['id']} at {str(last['sha'])[:7]} = {last['conclusion']} ({last['url']})"
                if last else "no completed run on this branch")
        print(f"ci-status: pending for HEAD ({head[:7]}) — {tail}")
        return 1
    print(f"ci-status: RED on {head[:7]} — run {info['head_run']['id']} = {info['head_run']['conclusion']} "
          f"({info['head_run']['url']})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

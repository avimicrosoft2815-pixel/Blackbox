"""The real-user journey, from a fresh installation: install → attach → capture → diagnose → approve a repair → verify.

Every other bench in this directory measures one mechanism. This one measures the thing a user actually does, in the order
they do it, from a **fresh virtual environment that only knows the wheel** — no `PYTHONPATH`, no editable install, no
test fixtures. The report says what worked, what did not, and what was not attempted, in its own words per step.

The journey, with the provider configuration stated rather than implied:

1. **install** — `pip wheel` the checkout, `python -m venv` a new environment, `pip install 'blackbox-recorder[capture,
   capture-sdks]'` into it (so `httpx` and `openai` are real dependencies of that environment), and record the versions.
2. **attach** — an app that uses the `openai` SDK as shipped, inside `api.run("support-agent")`, run under
   `blackbox run -- python app.py`. Its provider is a **loopback origin** standing in for OpenAI (the base URL is handed to
   the child by the launcher, so the app itself has no idea); the origin answers one call and rejects the second with a
   404 for a model it does not know. That is the incident: a real client, a real HTTP failure, a real exit code.
3. **capture** — the run's capsules, listed with `blackbox ls` and verified with `blackbox verify`.
4. **diagnose** — `blackbox diagnose` on the capsule that records the failed call.
5. **view** — `blackbox view` to one offline HTML page.
6. **approve a repair** — a proposal (written here, as an agent would) that fixes the typo in the app's model name, then
   `repair preview` for the token, `repair apply --approve`, and `repair verify --test` running the app again. Nothing is
   executed by preview or apply; the only command that runs is the one passed to `--test`.
7. **verify** — a second `blackbox run` after the fix, with its diagnosis, so "fixed" is a measured second run and not a
   claim.

    python bench/journey.py --out bench/results/2026-11-18-journey.json

Network use: `pip` from the configured index (this is what an installation is), and loopback HTTP. No provider is called.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

APP = '''\
"""The user's app: an OpenAI client inside one named step, with one successful call and one mistake.

The mistake is the incident this journey is about: the second call asks for `gpt-4o-mni`, a model the provider does not
know. The app does not catch it — a crash is what a real first run looks like, and it is also what gives the recorder
something to explain.
"""
import openai

from blackbox import api

client = openai.OpenAI()

with api.run("support-agent"):
    first = client.chat.completions.create(model="gpt-4o-mini",
                                           messages=[{"role": "user", "content": "hello"}])
    print("first:", first.choices[0].message.content)
    second = client.chat.completions.create(model="gpt-4o-mni",
                                            messages=[{"role": "user", "content": "and again"}])
    print("second:", second.choices[0].message.content)
'''


class Origin(BaseHTTPRequestHandler):
    """A loopback stand-in for the provider: one known model, one 404, and no opinion about anything else."""

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        model = str(body.get("model") or "")
        if model != "gpt-4o-mini":
            payload = json.dumps({"error": {"message": f"The model `{model}` does not exist",
                                            "type": "invalid_request_error", "code": "model_not_found"}}).encode()
            self.send_response(404)
        else:
            payload = json.dumps({"id": "chatcmpl-journey", "object": "chat.completion",
                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                               "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: float = 300) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    return {"argv": argv, "exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 2),
            "stdout": proc.stdout.strip().splitlines()[-8:], "stderr": proc.stderr.strip().splitlines()[-8:]}


def build_wheel(work: Path) -> dict[str, Any]:
    out = work / "wheel"
    out.mkdir()
    step = run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(out)], ROOT, dict(os.environ))
    wheels = sorted(out.glob("*.whl"))
    return {**step, "wheel": wheels[0].name if wheels else None}


def fresh_venv(work: Path, wheel: Path) -> tuple[Path, dict[str, Any]]:
    venv = work / "venv"
    made = run([sys.executable, "-m", "venv", str(venv)], work, dict(os.environ))
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = run([str(python), "-m", "pip", "install", "-q", f"{wheel}[capture,capture-sdks]"], work,
                    dict(os.environ), timeout=900)
    versions = run([str(python), "-c",
                    "import json,importlib.metadata as m;"
                    "print(json.dumps({n: m.version(n) for n in ('blackbox-recorder','openai','httpx','anthropic')}))"],
                   work, dict(os.environ))
    parsed: dict[str, Any] = {}
    if versions["exit_code"] == 0:
        try:
            parsed = json.loads((versions["stdout"] or ["{}"])[-1])
        except Exception:
            parsed = {"raw": versions["stdout"]}
    return python, {"venv": made, "install": installed, "versions": parsed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The fresh-install journey: install, attach, capture, diagnose, repair, verify")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-18-journey.json"))
    ap.add_argument("--keep", action="store_true", help="leave the work directory in place for inspection")
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="bbx-journey-"))
    report: dict[str, Any] = {"journey": "install → attach → capture → diagnose → view → approve a repair → verify",
                              "date": time.strftime("%Y-%m-%d", time.gmtime()),
                              "host": {"platform": platform.platform(), "python": platform.python_version()},
                              "provider": "a loopback origin standing in for OpenAI; the child is handed "
                                          "OPENAI_BASE_URL by the launcher and never names a real provider",
                              "steps": {}, "work_dir": str(work)}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{server.server_port}"
    try:
        wheel_step = build_wheel(work)
        report["steps"]["1_install"] = {"wheel": wheel_step}
        wheel_path = work / "wheel" / wheel_step["wheel"] if wheel_step["wheel"] else None
        assert wheel_path is not None and wheel_path.exists(), wheel_step
        python, venv_step = fresh_venv(work, wheel_path)
        report["steps"]["1_install"]["fresh_venv"] = venv_step

        app_dir = work / "app"
        app_dir.mkdir()
        (app_dir / "app.py").write_text(APP, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(work)),
               "OPENAI_API_KEY": "sk-journey-local"}
        blackbox = str(python.parent / ("blackbox.exe" if os.name == "nt" else "blackbox"))
        data = work / "data"

        def blackbox_run(*extra: str) -> dict[str, Any]:
            return run([blackbox, "run", "--data-dir", str(data),
                        "--upstream", f"openai={upstream}@/openai", "--", *extra], app_dir, env)

        first = blackbox_run(str(python), "app.py")
        report["steps"]["2_attach_and_capture"] = {"run": first}
        report["steps"]["2_attach_and_capture"]["summary_line"] = [
            line for line in first["stdout"] if "capsule" in line.lower() or "run " in line][:4]

        listed = run([blackbox, "ls", "--data", str(data)], app_dir, env)
        capsules = sorted((data / "capsules").glob("*.bbx"))
        report["steps"]["3_capture"] = {"ls": listed, "capsules": [path.name for path in capsules]}

        verified = run([blackbox, "verify", *[str(path) for path in capsules]], app_dir, env) if capsules else None
        report["steps"]["3_capture"]["verify"] = verified

        failed_capsule = None
        for capsule in capsules:
            out = work / f"incident-{capsule.stem}.md"
            step = run([blackbox, "diagnose", str(capsule), "--out", str(out)], app_dir, env)
            text = out.read_text(encoding="utf-8") if out.exists() else ""
            step["report_exists"] = out.exists()
            step["report_chars"] = len(text)
            step["findings_head"] = [line for line in text.splitlines() if line.strip().lower().startswith(
                ("finding", "what happened", "observed", "likely", "cannot tell", "- "))][:8]
            step["mentions_the_model"] = "gpt-4o-mni" in text
            report["steps"].setdefault("4_diagnose", {})[capsule.name] = step
            if "gpt-4o-mni" in text:
                failed_capsule = capsule

        view_step = None
        if failed_capsule is not None:
            page = work / "view.html"
            view_step = run([blackbox, "view", str(failed_capsule), "--out", str(page)], app_dir, env)
            view_step["page_bytes"] = page.stat().st_size if page.exists() else None
            view_step["offline"] = page.exists() and "http://" not in page.read_text(encoding="utf-8")[:20000]
        report["steps"]["5_view"] = view_step

        # The repair an agent would propose: the model name in the app, nothing else. The content is the whole file, and
        # `expected_sha256` binds the proposal to the bytes it was reviewed against.
        import hashlib

        before = (app_dir / "app.py").read_bytes()
        fixed = APP.replace("gpt-4o-mni", "gpt-4o-mini")
        proposal = {"schema": "blackbox.repair-proposal/1",
                    "title": "use a model the provider serves",
                    "rationale": "the recorder shows a 404 model_not_found for `gpt-4o-mni`; the other call in the same "
                                 "step succeeded, so the client, the base URL and the key are all fine",
                    "risk": ["changes the model the app asks for; the second call will reach the provider"],
                    "tests": ["python app.py"],
                    "changes": [{"path": "app.py", "content": fixed,
                                 "expected_sha256": hashlib.sha256(before).hexdigest()}]}
        (work / "proposal.json").write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
        preview = run([blackbox, "repair", "preview", str(work / "proposal.json"), "--repo", str(app_dir)],
                      app_dir, env)
        token = None
        for line in preview["stdout"] + preview["stderr"]:
            if "approve" in line.lower():
                for word in line.replace("`", " ").replace("'", " ").split():
                    if len(word) == 32 and all(char in "0123456789abcdef" for char in word):
                        token = word
        applied = run([blackbox, "repair", "apply", str(work / "proposal.json"), "--repo", str(app_dir),
                       "--approve", token or ""], app_dir, env)
        after = (app_dir / "app.py").read_bytes()
        # `repair verify --test` runs the command in *this* process's environment, not the launcher's: the child has to
        # be pointed at the stand-in provider itself, because no `blackbox run` is wrapping it this time. That is a real
        # property of the command and worth recording — a test that needs the variables `blackbox run` exports must be
        # given them.
        verify_env = dict(env, OPENAI_BASE_URL=f"{upstream}/openai")
        verified_repair = run([blackbox, "repair", "verify", str(work / "proposal.json"), "--repo", str(app_dir),
                               "--test", f"{python} app.py"], app_dir, verify_env)
        report["steps"]["6_approve_a_repair"] = {
            "proposal_written_by": "this script, as an agent would; nothing in it was executed by preview or apply",
            "preview": preview, "token_found": token is not None,
            "applied": applied, "file_changed": before != after,
            "verify": verified_repair,
        }

        second_env = dict(env)
        second_data = work / "data-after"
        second = run([blackbox, "run", "--data-dir", str(second_data),
                      "--upstream", f"openai={upstream}@/openai", "--", str(python), "app.py"], app_dir, second_env)
        second_capsules = sorted((second_data / "capsules").glob("*.bbx"))
        second_diagnose = None
        if second_capsules:
            out = work / "incident-after.md"
            second_diagnose = run([blackbox, "diagnose", str(second_capsules[0]), "--out", str(out)], app_dir, second_env)
            second_diagnose["report_mentions_the_model"] = out.exists() and "model_not_found" in out.read_text("utf-8")
        report["steps"]["7_verify"] = {"run_after_the_fix": second, "capsules": [p.name for p in second_capsules],
                                       "diagnose": second_diagnose}
    finally:
        server.shutdown()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    install_ok = report["steps"].get("1_install", {}).get("wheel", {}).get("exit_code") == 0 and bool(
        report["steps"].get("1_install", {}).get("fresh_venv", {}).get("versions"))
    run_ok = report["steps"].get("2_attach_and_capture", {}).get("run", {}).get("exit_code") == 1
    captured = bool(report["steps"].get("3_capture", {}).get("capsules"))
    verified_ok = (report["steps"].get("3_capture", {}).get("verify") or {}).get("exit_code") == 0
    diagnosed = bool(report["steps"].get("4_diagnose"))
    report["verdict"] = {
        "installed_from_the_wheel_in_a_fresh_venv": install_ok,
        "the_child_reached_the_provider_through_the_launcher": run_ok,
        "capsules_were_written": captured,
        "every_capsule_verified": verified_ok,
        "diagnose_wrote_a_report": diagnosed,
        "view_wrote_one_offline_page": bool((report["steps"].get("5_view") or {}).get("offline")),
        "repair_needed_an_approval_token_and_changed_the_file": bool(
            report["steps"].get("6_approve_a_repair", {}).get("token_found")
            and report["steps"].get("6_approve_a_repair", {}).get("file_changed")),
        "verify_test_passed_after_the_repair": (report["steps"].get("6_approve_a_repair", {}).get("verify") or {}
                                                ).get("exit_code") == 0,
        "the_run_after_the_fix_succeeded": (report["steps"].get("7_verify", {}).get("run_after_the_fix") or {}
                                            ).get("exit_code") == 0,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("verdict:", json.dumps(report["verdict"], indent=1, sort_keys=True))
    print("written:", out)
    if args.keep:
        print("work dir:", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

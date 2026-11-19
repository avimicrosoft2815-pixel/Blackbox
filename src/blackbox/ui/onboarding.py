"""First-run onboarding: what this machine can do, what will be recorded, and the next click.

The desktop app's first screen answers four questions a new user actually has, in this order:

1. **Is this going to work here?** — the Python runtime, the window stack, the data directory, free space.
2. **What will BlackBox record, and what will it not?** — stated as two lists, not as a promise in prose.
3. **How do I record my app?** — the installed SDKs are detected and named, with the exact command for the one the user
   has; a provider key is reported as *present* or *absent* and its **value is never read, shown or sent**.
4. **Where does it all live?** — the data directory, its size, and the fact that nothing leaves the machine.

Three rules this module keeps, because a first-run screen is where a product is most tempted to overclaim:

* **every check is an observation**, with the value it observed: `python 3.11.9`, `data dir writable`, `no key found` —
  never "everything is fine".
* **every failure is actionable**: a check that is not `ok` carries a command a person can paste, or a sentence naming
  what to install. There is no dead end that requires a terminal to *understand*, which is the directive's bar.
* **no secret is read.** The provider checks look at whether an environment variable **exists**; they never format its
  value, and a test greps the rendered page for a planted key to prove it.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from typing import Any

from blackbox import __version__

#: Providers this build can name, and the environment variables a user normally has for them. The **names** are used;
#: the values are never read. `(provider, [env names], the SDK that would carry it, model family to show)`
PROVIDERS: tuple[tuple[str, tuple[str, ...], str | None, str], ...] = (
    ("openai", ("OPENAI_API_KEY",), "openai", "gpt-*"),
    ("anthropic", ("ANTHROPIC_API_KEY",), "anthropic", "claude-*"),
    ("gcp.gemini", ("GOOGLE_API_KEY", "GEMINI_API_KEY"), None, "gemini-*"),
    ("gcp.vertex_ai", ("GOOGLE_APPLICATION_CREDENTIALS",), None, "vertex endpoints"),
)

#: SDKs the recorder can attach to without the user writing a line of code, and what each needs. Keep this in step with
#: `cap_sdk/` — a name here that the seam does not patch would be a promise the product does not keep.
SDKS: tuple[tuple[str, str], ...] = (("openai", "openai"), ("anthropic", "anthropic"), ("httpx", "httpx"))

#: What is recorded, and what is not. These two lists are the honest core of the onboarding page: the product's whole
#: claim is that a user can tell what is in the capsule before they record anything.
CAPTURED: tuple[str, ...] = (
    "every model call the recorder can see: provider, model name, request and response status, timing, sizes",
    "the tree of nodes and steps your code opens with `blackbox.run` / `blackbox.step`",
    "stream chunks as they arrive, including a stream the provider cut short (recorded as partial, never as complete)",
    "tool calls and their results, and the errors that ended a run",
    "gaps the recorder knows about: a lost sequence, a truncated segment, a dropped frame",
)
NOT_CAPTURED: tuple[str, ...] = (
    "nothing leaves this machine: no account, no cloud, no telemetry, no update check",
    "prompts and completions are recorded **only** under your content policy; the default redacts secrets and hashes "
    "what it cannot judge safe",
    "other programs' traffic, other users' sessions, or any process you did not start with `blackbox run`",
    "browser AI websites: this build records SDK and HTTP clients, not a browser extension",
    "your source code — a capsule holds what happened, not what your program is",
)


def _check(check_id: str, severity: str, title: str, detail: str, *, command: str = "",
           docs: str = "") -> dict[str, Any]:
    return {"id": check_id, "severity": severity, "title": title, "detail": detail, "command": command, "docs": docs}


def _python_check() -> dict[str, Any]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    # `requires-python` is >= 3.11, so every interpreter that loads this package passes; the check stays because the
    # desktop shell can be started by an older `python3` on a machine with several, and that is worth saying out loud
    # rather than failing at an import three frames deep.
    return _check("python", "ok", "Python runtime", f"{version} at {sys.executable}")


def _window_check() -> dict[str, Any]:
    from blackbox.ui import desktop

    report = desktop.selftest()
    if report["available"]:
        return _check("window", "ok", "Desktop window", f"{report['window_backend']} on {report['platform']}")
    return _check("window", "warn", "The window toolkit is incomplete",
                  f"missing: {', '.join(report['missing'])}", command=str(report["install_hint"]),
                  docs="docs/desktop.md")


def _data_dir_check(data_dir: str) -> dict[str, Any]:
    path = os.path.abspath(data_dir)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        return _check("data_dir", "fail", "The data directory cannot be created", f"{path}: {exc}",
                      command="BLACKBOX_HOME=/a/directory/you/can/write blackbox desktop")
    probe = os.path.join(path, ".blackbox-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
    except OSError as exc:
        return _check("data_dir", "fail", "The data directory is not writable", f"{path}: {exc}",
                      command="chmod u+w " + path)
    try:
        free = shutil.disk_usage(path)
        free_gb = free.free / (1024 ** 3)
        detail = f"{path} — {free_gb:.1f} GB free"
        if free_gb < 1.0:
            return _check("data_dir", "warn", "Almost no space left for recordings", detail,
                          command="blackbox retention   # explains every deletion, deletes nothing")
    except OSError as exc:
        detail = f"{path} — free space unknown ({exc})"
    # Measured, not assumed: walk what is actually on disk and report any path another user on this machine could read.
    from blackbox.platform import perms as perms_mod

    audit = perms_mod.audit(path)
    findings = list(audit["findings"])                                   # type: ignore[call-overload]
    if findings:
        first = findings[0]
        shown = ", ".join(str(entry.get("mode") or entry.get("broad_principals")) for entry in findings[:3])
        return _check("data_dir", "warn", "Recordings are readable by other users on this machine",
                      f"{path} — {len(findings)} of {audit['paths_checked']} path(s) are looser than "
                      f"0700/0600 ({shown}); the first is {first['path']}",
                      command=f"chmod -R go-rwx {path}" if audit["platform"] != "win32"
                      else f"icacls {path} /inheritance:r /grant:r %USERNAME%:(OI)(CI)F",
                      docs="docs/desktop.md")
    detail += f" — permissions {audit['platform']}: " + (
        "0700/0600 on every path checked" if not audit["truncated"]
        else f"first {audit['paths_checked']} paths checked, all private")
    return _check("data_dir", "ok", "Recordings live here", detail)


def _sdk_checks(find_spec: Any = None) -> list[dict[str, Any]]:
    probe = find_spec or importlib.util.find_spec
    out = []
    for name, module in SDKS:
        try:
            present = probe(module) is not None
        except (ImportError, ValueError):
            present = False
        if present:
            out.append(_check(f"sdk:{name}", "ok", f"{name} is installed",
                              "calls made with it are recorded when you start your app through `blackbox run`"))
        else:
            out.append(_check(f"sdk:{name}", "action", f"{name} is not installed",
                              "BlackBox records it without you changing your code — but it has to be importable",
                              command=f"pip install {module}"))
    return out


def _provider_checks(env: dict[str, str] | None = None, find_spec: Any = None) -> list[dict[str, Any]]:
    environ = os.environ if env is None else env
    probe = find_spec or importlib.util.find_spec
    out = []
    for provider, names, sdk, family in PROVIDERS:
        present = [name for name in names if environ.get(name)]
        if present:
            out.append(_check(f"provider:{provider}", "ok", f"{provider} key found",
                              f"{present[0]} is set (its value is never read by BlackBox). Model family: {family}"))
        elif sdk:
            try:
                sdk_present = probe(sdk) is not None
            except (ImportError, ValueError):
                sdk_present = False
            out.append(_check(f"provider:{provider}", "action", f"no {provider} key found",
                              f"{names[0]} is not set" + ("" if sdk_present else f", and {sdk} is not installed"),
                              command=f"export {names[0]}=…   # then: blackbox run -- python3 app.py"))
        else:
            out.append(_check(f"provider:{provider}", "info", f"{provider} not configured",
                              f"{names[0]} is not set; this build names the provider only when it sees the address"))
    return out


def _runs_check(data_dir: str) -> dict[str, Any]:
    from blackbox.ui import library

    found = library.scan(data_dir, limit=1)
    if found.counts.get("capsules"):
        return _check("runs", "ok", "There are recordings to open",
                      f"{found.counts['capsules']} capsule(s), {found.counts.get('runs', 0)} run(s)")
    return _check("runs", "action", "Nothing recorded yet",
                  "start your app through BlackBox once and a capsule appears here",
                  command="blackbox run -- python3 app.py",
                  docs="docs/desktop.md")


def probe(data_dir: str, *, env: dict[str, str] | None = None, find_spec: Any = None) -> dict[str, Any]:
    """Every first-run observation, the two lists, and a summary the page leads with."""
    checks = [_python_check(), _window_check(), _data_dir_check(data_dir)]
    checks.extend(_sdk_checks(find_spec))
    checks.extend(_provider_checks(env, find_spec))
    checks.append(_runs_check(data_dir))
    severities = [check["severity"] for check in checks]
    if "fail" in severities:
        summary = ("This machine cannot record yet — the checks marked red below have the command that fixes them.",
                   "blocked")
    elif "action" in severities:
        summary = ("BlackBox works here. The steps below name what it can record and what it still needs.", "ready")
    else:
        summary = ("Everything this build needs is present. Record something, then open it here.", "complete")
    return {
        "blackbox": __version__,
        "data_dir": os.path.abspath(data_dir),
        "platform": {"system": sys.platform, "machine": os.uname().machine if hasattr(os, "uname") else ""},
        "checks": checks,
        "counts": {severity: severities.count(severity) for severity in ("ok", "action", "warn", "info", "fail")},
        "summary": summary[0],
        "state": summary[1],
        "captured": list(CAPTURED),
        "not_captured": list(NOT_CAPTURED),
    }

"""What a temporary `sitecustomize.py` calls at interpreter startup, so `blackbox run -- python app.py` records
without the application importing anything.

The technique is the one `opentelemetry-instrument` uses: a directory on `PYTHONPATH` holding a `sitecustomize.py`, which
`site` imports by name before `__main__`. This module is the body that file calls; the file itself is a few lines and is
written per run by `rec/launcher.py`.

**A `.pth` does not work here, and the plan's shorthand conflates the two.** `site` processes `.pth` files only inside
*known site directories* (`addsitedir` is what reads them); a `.pth` sitting in a directory that reached `sys.path` via
`PYTHONPATH` is never opened. The first version of this module was wired that way, and
`tests/test_launcher_attach.py::test_sitecustomize_installs_the_capture_layer_without_the_application_importing_anything`
failed with `autoimport_ran is False` — which is why the test exists, and why the mechanism is *named in the run result*
instead of being assumed. The plan's two spellings ("`.pth` in a temp site dir / `PYTHONPATH`") are the same thing in
Python only when the temp dir is registered as a site directory, which needs `-S`-defeating gymnastics.

The cost of this mechanism is real, so it is handled rather than hidden: only one module named `sitecustomize` can win,
so a directory that already had one — a distro hook, another agent's instrumentation — would be shadowed. `delegate()`
therefore runs the next `sitecustomize` on `sys.path` before this one installs, so both run, in the order the user's own
`PYTHONPATH` implies. `test_an_existing_sitecustomize_is_chained_not_replaced` is that promise.

Three rules, each one a test:

* **temporary, never global.** the directory is created per child, is on that child's `PYTHONPATH`, and deleted when the
  child exits — including when it was killed. Nothing is written to `site-packages`, `~/.local/lib`, or any interpreter's
  own directories. A recorder that edits a user's Python installation is a package manager, and it cannot be uninstalled
  cleanly.
* **a failure costs the app nothing.** A start-up traceback from a recorder is how a tool gets uninstalled. Every
  exception is swallowed here and recorded in `last_result()` for the CLI to report.
* **silence on stdout.** The child's stdout belongs to the child; diagnostics go to stderr and only under
  `BLACKBOX_VERBOSE`.
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import sys
from typing import Any

#: The variable the launcher sets to say "this child was given an auto-import". A child can set it to `0` to opt out
#: without the launcher's help; `--no-sdk` is the launcher-side spelling of the same decision.
ENV_SDK = "BLACKBOX_SDK"
#: The file the launcher writes into its temp directory, and the whole of its content. `site` imports it by name from
#: `sys.path`, so it may not import anything of its own before `blackbox` — hence the explicit `os` import first.
SITECUSTOMIZE_NAME = "sitecustomize.py"
SITECUSTOMIZE_BODY = """\
# Written by `blackbox run` for one child process; deleted when that child exits.
import os

import blackbox.cap_sdk.autoimport as _blackbox_autoimport

_blackbox_autoimport.delegate(os.path.dirname(os.path.abspath(__file__)))
_blackbox_autoimport.activate()
"""
#: Recorded in the run result so a reader can see *which* mechanism ran, rather than inferring it.
MECHANISM = "sitecustomize on PYTHONPATH (the opentelemetry-instrument technique)"
FALSY = frozenset({"0", "false", "no", "off"})
TRUTHY = frozenset({"1", "true", "yes", "on"})

_last: dict[str, Any] = {"installed": False, "reason": "not activated", "activated": False}


def _verbose() -> bool:
    return os.environ.get("BLACKBOX_VERBOSE", "").strip().lower() in TRUTHY


def _say(message: str) -> None:
    """stderr only, and only when asked: stdout belongs to the application."""
    if _verbose():
        print(f"blackbox sdk: {message}", file=sys.stderr)


def last_result() -> dict[str, Any]:
    """What the last `activate()` did — for a launcher that wants to report it, and for the tests."""
    return dict(_last)


def activate() -> dict[str, Any]:
    """Install the capture layer in this interpreter. Never raises; returns what it decided and why. Idempotent."""
    global _last
    if _last.get("activated"):
        return _last
    if os.environ.get(ENV_SDK, "").strip().lower() in FALSY:
        _last = {"installed": False, "reason": f"{ENV_SDK}=0", "activated": True}
        return _last
    try:
        from blackbox import api
    except Exception as exc:                              # a `sitecustomize` in a half-installed tree
        _last = {"installed": False, "reason": f"{type(exc).__name__}: {exc}", "activated": True}
        _say(f"not installed: {_last['reason']}")
        return _last
    try:
        status = api.install()
    except Exception as exc:                              # install() is fail-open, but do not bet a start-up on it
        _last = {"installed": False, "reason": f"{type(exc).__name__}: {exc}", "activated": True}
        _say(f"not installed: {_last['reason']}")
        return _last
    installed = bool(status.get("enabled"))
    _last = {"installed": installed, "reason": status.get("disabled_reason") or "installed",
             "data_dir": status.get("data_dir"), "writer_uuid": status.get("writer_uuid"),
             "runs_started": status.get("runs_started"), "activated": True}
    if installed:
        # The application never asked for this, so it cannot be expected to close it. `atexit` runs before the
        # interpreter tears down, which is the last moment a frame can still be written cleanly.
        atexit.register(_close_at_exit)
    _say(f"installed ({_last['reason']})")
    return _last


def delegate(exclude_dir: str) -> str | None:
    """Run the next `sitecustomize` on `sys.path`, if there is one, so installing this one does not replace it.

    Returns the path that was run, or `None`. Guarded against running a second copy of *this* hook, which is the only
    way the chain could recurse.
    """
    if os.environ.get("BLACKBOX_SITECUSTOMIZE_CHAINED"):
        return None
    os.environ["BLACKBOX_SITECUSTOMIZE_CHAINED"] = "1"
    try:
        mine = os.path.realpath(os.path.join(os.path.abspath(exclude_dir), SITECUSTOMIZE_NAME))
    except OSError:                                       # pragma: no cover
        return None
    for entry in sys.path:
        if not entry:
            continue
        candidate = os.path.join(entry, SITECUSTOMIZE_NAME)
        try:
            if not os.path.isfile(candidate) or os.path.realpath(candidate) == mine:
                continue
        except OSError:                                   # pragma: no cover - unreadable path entry
            continue
        spec = importlib.util.spec_from_file_location("_blackbox_chained_sitecustomize", candidate)
        if spec is None or spec.loader is None:           # pragma: no cover
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:                          # the other hook's failure is not ours to escalate
            _say(f"the sitecustomize at {candidate} raised {type(exc).__name__}: {exc}")
        return candidate
    return None


def _close_at_exit() -> None:
    try:
        from blackbox import api

        api.shutdown()
    except Exception:                                     # an exit path may not raise
        pass


__all__ = ["ENV_SDK", "MECHANISM", "SITECUSTOMIZE_BODY", "SITECUSTOMIZE_NAME", "activate", "delegate", "last_result"]

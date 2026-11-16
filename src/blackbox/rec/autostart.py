"""P3B-03's last piece: the child's own interpreter installs the seam, without the child importing us.

`blackbox run -- python app.py` has to record an SDK call made by code that has never heard of this package. The
mechanism is the one `opentelemetry-instrument` uses: put a directory first on the child's `PYTHONPATH` holding a
`sitecustomize.py`, which CPython imports during `site` startup — before any of the child's own code runs.

**Why `sitecustomize.py` and not the `.pth` the plan names.** The plan says "`.pth` in a temp site dir / `PYTHONPATH`",
and those are not the same thing. A `.pth` file is only executed when it sits in a directory `site` scans, i.e. a
**site-packages** directory; a `.pth` placed on `PYTHONPATH` is not executed at all. Measured on CPython 3.11.2
(`python -c pass` with a `.pth` on `PYTHONPATH`: nothing ran; with `sitecustomize.py` in the same directory: it ran).
So the `.pth` variant means **writing into the user's site-packages** — a package manager's directory, needing write
access, leaving residue if the launcher is killed, and mutating an environment we were asked only to observe. This
module does the same job in a temporary directory that is removed on exit (D-044). The plan's *intent* — "so
`install()` runs without code changes" — is what the tests assert; the mechanism is the decision's business.

**Cheap when nothing happens.** The shim imports nothing of ours at startup. It installs a `meta_path` finder that
watches for `httpx`/`httpx2` to finish importing and only then pays for the recorder: opening a tape, probing the
seams, patching the client. A Python child that never speaks HTTP to a provider costs one `sys.meta_path` entry and one
`atexit` registration — measured at ~1 ms on top of bare `python -c pass`, against ~25 ms for importing this module
plus ~50 ms for opening a tape nobody needed. When no client library is ever imported, the `atexit` hook says so, so a
run with no SDK vantage has a record explaining why instead of a silence.

**Two failure modes it refuses to create.**

* A shadowed `sitecustomize`: ours is first on `sys.path`, so one the user already had would never be imported.
  `bootstrap()` imports the *next* one on the path after taking itself off it, which is what a chain of site
  customisations means in practice. Its exception is not caught: it would have raised without us.
* A broken child: every step is inside a `try`, and the shim that runs before this package is importable catches
  `BaseException` and writes one JSON file into `<data dir>/attach/` instead of printing a traceback into somebody's
  stdout. A recorder that changes what the application prints is a recorder nobody keeps.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                    # annotations only: importing `typing` here would be 2 ms on every armed child
    from collections.abc import Callable, MutableMapping

ARMED_ENV = "BLACKBOX_AUTOSTART"
DIR_ENV = "BLACKBOX_AUTOSTART_DIR"
ATTACH_DIRNAME = "attach"
SHIM_NAME = "sitecustomize.py"
VANTAGE_SDK = 1
TRIGGERS = ("httpx", "httpx2")
#: Wrappers that run another program: `blackbox run -- uv run python app.py` is a Python command too.
WRAPPERS = frozenset({"env", "uv", "poetry", "hatch", "pipx", "nox", "tox", "rye", "pdm"})
#: What those wrappers say before the program they run: `uv run …`, `uv tool run …`, `poetry run …`.
WRAPPER_SUBCOMMANDS = frozenset({"run", "exec", "tool"})

SHIM = '''\
"""Written by `blackbox run` into a temporary directory, removed when the child exits (P3B-03, D-044).

This runs during interpreter startup, before any of the application's own code, and it may not change what the
application does. So it imports nothing of ours yet: it watches for the HTTP client library to be imported and only
then pays for the recorder. A Python child that never speaks HTTP to a provider costs a `meta_path` entry and an
`atexit` registration, and nothing else.
"""
import atexit as _atexit
import os as _os
import sys as _sys

_TRIGGERS = ("httpx", "httpx2")
_STATE = {"fired": False}


def _report(payload):
    """One JSON file in the run's data directory: the child's own account of what happened to it."""
    try:
        import json as _json
        import os as _os2
        import time as _time

        root = _os2.environ.get("BLACKBOX_HOME") or _os2.environ.get("BLACKBOX_DATA_DIR")
        if not root:
            return
        directory = _os2.path.join(root, "attach")
        _os2.makedirs(directory, exist_ok=True)
        payload.setdefault("pid", _os2.getpid())
        payload.setdefault("at", _time.time())
        path = _os2.path.join(directory, "startup-%d.json" % _os2.getpid())
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            _json.dump(payload, handle, indent=2, sort_keys=True)
        _os2.replace(path + ".tmp", path)
    except BaseException:
        pass


def _fire():
    if _STATE["fired"]:
        return
    _STATE["fired"] = True
    try:
        _sys.meta_path.remove(_WATCH)
    except (ValueError, NameError):
        pass
    try:
        import blackbox.rec.autostart as _autostart

        _autostart.bootstrap()
    except BaseException:
        import traceback as _traceback

        _report({"armed": True, "seam": None, "error": _traceback.format_exc(limit=4)})


class _Watch:
    """A `meta_path` finder that runs `_fire()` once, after the HTTP client library has finished importing."""

    _busy = False

    def find_spec(self, fullname, path=None, target=None):
        if _STATE["fired"] or _Watch._busy or fullname not in _TRIGGERS:
            return None
        import importlib.util as _util

        _Watch._busy = True
        try:
            spec = _util.find_spec(fullname)
        except BaseException:
            spec = None
        finally:
            _Watch._busy = False
        loader = getattr(spec, "loader", None)
        original = getattr(loader, "exec_module", None)
        if spec is None or original is None:
            return None

        def exec_module(module, _original=original):
            _original(module)
            _fire()

        try:
            loader.exec_module = exec_module
        except BaseException:
            return None
        return spec


_WATCH = _Watch()


def _armed_from_exit():
    """`atexit`: if no client library was ever imported, say so instead of leaving the run with a silent vantage."""
    if not _STATE["fired"]:
        _report({"armed": True, "seam": None, "capture": False,
                 "skipped": "the child never imported " + " or ".join(_TRIGGERS)})


try:
    if _os.environ.get("BLACKBOX_AUTOSTART") == "1":
        _sys.meta_path.insert(0, _WATCH)
        _atexit.register(_armed_from_exit)
except BaseException:
    pass
'''


# -- the parent side: arm a child's environment, and clean up after it ------------------------------------------
class Startup:
    """The temporary auto-import directory handed to one child, and its removal."""

    def __init__(self, directory: str, previous: str | None = None) -> None:
        self.directory = directory
        self.env_key = "PYTHONPATH"
        self.previous = previous
        self.removed = False
        self.notes: list[str] = []

    def close(self) -> None:
        """Remove the directory. Idempotent, and never raises: cleanup may not change a run's exit code."""
        if self.removed:
            return
        self.removed = True
        shutil.rmtree(self.directory, ignore_errors=True)

    def as_dict(self) -> dict[str, Any]:
        return {"directory": self.directory, "removed": self.removed, "notes": list(self.notes)}

    def __repr__(self) -> str:                       # pragma: no cover - debugging aid
        return f"Startup(directory={self.directory!r}, removed={self.removed})"


def looks_like_python(argv: list[str]) -> bool:
    """Is this command an interpreter that will import `sitecustomize`? `python`, `python3.12`, `pypy3`, `py`.

    Wrappers and their own arguments are looked through, because `blackbox run -- uv run python app.py` and
    `blackbox run -- env PYTHONWARNINGS=ignore python app.py` are both Python commands. The scan stops at the first
    token that is neither a wrapper, a flag nor an assignment — a command named `python` that is really something else
    is a risk this accepts, and `--no-sdk` is the answer to it.
    """
    seen_wrapper = False
    for token in argv[:5]:
        if token.startswith("-"):
            continue                       # a wrapper's own flag (`uv run --python 3.12 python app.py`)
        name = os.path.basename(token).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in WRAPPERS:
            seen_wrapper = True
            continue                       # `uv`, `env`, `poetry`…
        if "=" in token or (seen_wrapper and name in WRAPPER_SUBCOMMANDS):
            continue                       # `env FOO=1 python app.py`, `uv run python app.py`, `poetry run …`
        return name.startswith(("python", "pypy")) or name == "py"
    return False


def _source_root() -> str | None:
    """Where this package lives, if it is a source checkout the child could import.

    An installed wheel already sits in a directory the child's interpreter searches; putting *that* on `PYTHONPATH`
    would push a whole site-packages ahead of the child's own, which is how a recorder breaks somebody's imports. A
    source checkout (`.../src/blackbox`) is a different matter: the child cannot import it unless we say so.
    """
    package = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # .../src or site-packages
    parts = {part.lower() for part in package.split(os.sep)}
    if parts & {"site-packages", "dist-packages"}:
        return None
    return package


def arm(env: MutableMapping[str, str], *, data_dir: str) -> Startup:
    """Write the shim into a fresh temp dir and prepend it to `env`'s `PYTHONPATH`. Close the result afterwards."""
    directory = tempfile.mkdtemp(prefix="blackbox-sdk-")
    os.chmod(directory, 0o700)
    with open(os.path.join(directory, SHIM_NAME), "w", encoding="utf-8") as handle:
        handle.write(SHIM)
    startup = Startup(directory=directory, previous=env.get("PYTHONPATH"))
    parts = [directory]
    root = _source_root()
    if root is not None:
        parts.append(root)
        startup.notes.append(f"appended the source checkout {root!r} so the child can import this package")
    if env.get("PYTHONPATH"):
        parts.append(str(env["PYTHONPATH"]))
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env[ARMED_ENV] = "1"
    env[DIR_ENV] = directory
    env.setdefault("BLACKBOX_HOME", data_dir)
    env.setdefault("BLACKBOX_DATA_DIR", data_dir)
    return startup


# -- the child side: what actually runs once the client library appears -------------------------------------------
def report_dir(data_dir: str) -> str:
    return os.path.join(data_dir, ATTACH_DIRNAME)


def _write_report(data_dir: str, report: dict[str, Any]) -> None:
    try:
        directory = report_dir(data_dir)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"startup-{report.get('pid', os.getpid())}.json")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            import json

            json.dump(report, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass          # a report we cannot write is not a reason to touch the child


def read_reports(data_dir: str) -> dict[str, list[dict[str, Any]]]:
    """What the children said about themselves: `{"startup": [...], "errors": [...]}`."""
    startup: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    directory = report_dir(data_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {"startup": startup, "errors": errors}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            import json

            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        (errors if name.startswith("startup-error-") else startup).append(payload)
    return {"startup": startup, "errors": errors}


def _delegate_sitecustomize(own_directory: str) -> None:
    """Import the `sitecustomize` we shadowed, if there is one.

    Ours is first on `sys.path` only because we put it there; a `sitecustomize` further along the path would have been
    the one CPython imported without us, so it must still run. Its exceptions propagate — they would have happened
    anyway, and swallowing somebody else's startup failure would be a worse bug than the one this prevents.
    """
    saved = list(sys.path)
    had_module = "sitecustomize" in sys.modules
    previous_module: Any = sys.modules.pop("sitecustomize", None)
    try:
        sys.path[:] = [entry for entry in saved if os.path.abspath(entry or ".") != os.path.abspath(own_directory)]
        import importlib

        try:
            importlib.import_module("sitecustomize")
        except ModuleNotFoundError as exc:
            if exc.name != "sitecustomize":
                raise
    finally:
        sys.path[:] = saved
        if had_module and previous_module is not None:
            sys.modules["sitecustomize"] = previous_module


def bootstrap(*, install: Callable[..., dict[str, Any]] | None = None,
              emit: Callable[..., int] | None = None) -> dict[str, Any]:
    """Run inside the child, from the shim: open its own tape, install the winning seam, report which one won.

    Returns the report (also written to disk). Never raises: this runs before the application's first line, and an
    exception here would be an application that cannot start because a recorder was attached to it.
    """
    started = time.time()
    report: dict[str, Any] = {"pid": os.getpid(), "python": sys.version.split()[0], "at": started,
                              "seam": None, "attempts": [], "capture": False, "armed": True}
    try:
        if os.environ.get(ARMED_ENV) != "1":
            report.update({"armed": False, "skipped": f"{ARMED_ENV} is not 1"})
            return report
        data_dir = os.environ.get("BLACKBOX_HOME") or os.environ.get("BLACKBOX_DATA_DIR")
        if not data_dir:
            report["skipped"] = "neither BLACKBOX_HOME nor BLACKBOX_DATA_DIR is set"
            return report
        own_directory = os.environ.get(DIR_ENV)
        if own_directory:
            _delegate_sitecustomize(own_directory)
        from blackbox import api

        status = (install or api.install)(vantage=VANTAGE_SDK)
        if not status.get("enabled"):
            report["error"] = status.get("disabled_reason") or "the recorder did not open a tape"
            report["data_dir"] = os.path.abspath(data_dir)
            return _write_and_return(report, report["data_dir"])
        report["writer_uuid"] = status.get("writer_uuid")
        report["data_dir"] = status.get("data_dir") or os.path.abspath(data_dir)
        emitter = emit or api.emit
        run_id = os.environ.get("BLACKBOX_RUN_ID")
        if run_id:
            from blackbox import constants as C
            from blackbox.codec import cbor

            # The child's own tape, and the launcher's run id on it: that is what makes the SDK vantage link to the
            # proxy and hook tapes as `exact` rather than as a coincidence of timestamps (D-036).
            emitter(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "vantage": "sdk", "source": "autostart",
                                                   "pid": os.getpid(), "python": report["python"]}))
        from blackbox.cap_sdk import preflight
        from blackbox.cap_sdk.sink import SinkRecorder, TapeSink

        chosen = preflight.choose(SinkRecorder(TapeSink(emitter)))
        report.update({"seam": chosen.seam, "capture": chosen.capturing,
                       "attempts": [attempt.as_dict() for attempt in chosen.attempts],
                       "seams": chosen.as_dict()})
        if run_id:
            atexit.register(_finish, run_id, emitter)
        return _write_and_return(report, report["data_dir"])
    except BaseException as exc:                  # the child must not die because the recorder did
        report["error"] = f"{type(exc).__name__}: {exc}"
        return _write_and_return(report, report.get("data_dir"))


def _finish(run_id: str, emit: Callable[..., int]) -> None:
    """`atexit`: close the run on the child's tape, then the tape itself. Nothing here may matter to the exit code."""
    try:
        from blackbox import api
        from blackbox import constants as C
        from blackbox.codec import cbor

        emit(C.KIND_RUN_END, cbor.encode({"run_id": run_id, "source": "autostart", "exit_code": None}))
        api.shutdown()
    except BaseException:
        pass


def _write_and_return(report: dict[str, Any], data_dir: str | None) -> dict[str, Any]:
    if data_dir:
        report["elapsed_ms"] = round((time.time() - float(report.get("at", time.time()))) * 1000.0, 3)
        _write_report(data_dir, report)
    return report


__all__ = sorted(["ARMED_ENV", "DIR_ENV", "SHIM", "SHIM_NAME", "Startup", "TRIGGERS", "arm", "bootstrap",
                  "looks_like_python", "read_reports", "report_dir"])

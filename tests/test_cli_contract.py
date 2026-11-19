"""`docs/cli.md` against the binary: the command set, the `--json` column and the exit codes.

The row this file is written for is "`blackbox --help` ≤ 20 commands; every command has `--json` and a documented exit
code". Three claims, and each is checked where it can actually fail:

* **the set** — the table's commands equal `__main__.KNOWN`, so a command added without a contract row (or a row left
  behind by a removed command) fails here rather than in a user's script;
* **`--json`** — every command whose row says `--json` accepts the flag (its parser does not reject it), and the two
  exceptions are asserted as exceptions rather than skipped: `link` always prints JSON, `ingest` prints nothing;
* **exit codes** — the documented number is *measured* on a failing invocation for every command that has a failure mode
  reachable without a fixture. Where a code needs a real capsule (a drifted one, a failed check), the test says so and
  leaves it to the suite that owns that behaviour, instead of inventing a green cell.

Exit codes are the part of a CLI that scripts depend on, and the part prose drifts from first: `diagnose` returning `1`
for "nothing readable" where every other command returns `2` for the same thing is a real difference a caller has to
know, and it is in the table for that reason.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import __main__ as front_door

DOC = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
ROW = re.compile(r"^\| `([a-z]+)` \| (.+?) \| (.+?) \| (.+?) \|$", re.M)


def _rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for match in ROW.finditer(DOC):
        name, what, machine, codes = match.groups()
        rows[name] = {"what": what, "machine": machine.replace("`", ""), "codes": codes.replace("`", "")}
    return rows


def _run(argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "blackbox", *argv], capture_output=True, text=True, timeout=timeout,
                          env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
                               "HOME": str(Path.home())})


def test_the_table_names_exactly_the_commands_the_front_door_has() -> None:
    rows = _rows()
    assert len(rows) >= 14, rows
    assert set(rows) == set(front_door.KNOWN), (sorted(set(rows) - set(front_door.KNOWN)),
                                                sorted(set(front_door.KNOWN) - set(rows)))
    assert len(front_door.KNOWN) <= 20, "the gate says at most twenty commands"


@pytest.mark.parametrize("command", sorted(front_door.KNOWN))
def test_every_command_has_a_working_help(command: str) -> None:
    completed = _run([command, "--help"])
    assert completed.returncode == 0, completed.stderr[-800:]
    # `status` has no parser of its own: `blackbox status --help` is the front door's help, and says so by listing usage.
    assert "usage:" in completed.stdout


def test_the_json_column_is_true_of_every_command_it_names() -> None:
    rows = _rows()
    for command, row in rows.items():
        cell = row["machine"]
        if cell.startswith("--json"):
            if command in ("status", "run"):
                continue                    # checked by running them below; `--help` is the parser check
            assert "--json" in _run([command, "--help"]).stdout, f"{command} is documented as taking --json"
        elif "always JSON" in cell:
            assert command == "link", command
        elif "stdout belongs to the agent" in cell:
            assert command == "ingest", command
        else:
            raise AssertionError(f"{command}: unknown machine-output cell {cell!r}")


def test_status_json_names_the_format_and_the_commands() -> None:
    completed = _run(["status", "--json"])
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["version"] and payload["format_version"] >= 1
    assert set(payload["commands"]) == set(front_door.KNOWN)
    assert payload["stage"] == "pre-alpha"


def test_run_json_is_one_object_and_keeps_the_child_exit_code(tmp_path: Path) -> None:
    completed = _run(["run", "--no-proxy", "--no-sdk", "--no-hooks", "--data-dir", str(tmp_path),
                      "--json", "--", sys.executable, "-c", "import sys; print('child'); sys.exit(7)"])
    assert completed.returncode == 7, completed.stderr[-800:]     # documented: the child's code, verbatim
    assert "child" in completed.stdout, "the child's own stdout must not be swallowed by our JSON"
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["exit_code"] == 7 and payload["run_id"].startswith("r_")


def test_ingest_prints_one_line_and_never_fails_the_agent() -> None:
    completed = _run(["ingest", "--tool", "no-such-tool", "--event", "nope"], timeout=120)
    assert completed.returncode == 0, completed.stderr[-800:]      # documented: always 0, fail-open
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].endswith(".json"), (
        f"a hook's stdout belongs to the agent: one line naming the inbox file, not {completed.stdout!r}")


def test_the_documented_exit_codes_are_the_ones_a_failure_returns(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing")
    cases = {
        "view": (["view", missing], 2),
        "serve": (["serve", missing], 2),
        "diagnose": (["diagnose", missing], 1),        # documented as 1: nothing readable to diagnose
        "capsule": (["capsule", "verify", missing], 2),
        "verify": (["verify", missing], 2),
        "repair": (["repair", "preview", missing], 2),
        "link": (["link", missing, missing], 2),
        "index": (["index", "--data", missing], 2),
        "retention": (["retention", "--data", missing], 2),
        "ls": (["ls", "--data", missing], 0),          # documented as 0: an empty listing is an answer
        "hook": (["hook", "status"], 2),               # documented as 2: no adapter chosen
        "run": (["run"], 2),                           # documented as 2: a command is required
    }
    found = {}
    for command, (argv, want) in cases.items():
        completed = _run(argv, timeout=300)
        assert completed.returncode == want, f"{command}: expected {want}, got {completed.returncode}: " \
                                             f"{completed.stderr[-600:]}"
        found[command] = want
    rows = _rows()
    for command, code in found.items():
        codes = rows[command]["codes"]
        assert re.search(rf"(^|[\s,`]){code}([\s,`]|$)", codes), \
            f"{command} returns {code}, which its row omits: {codes!r}"

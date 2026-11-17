"""P4-05 — `blackbox ls`: which capsule do I open, and is anything wrong with it.

The quickstart used to say "the capsule is under `data/capsules/`", which is only useful to someone who already knows
that a run writes two capsules and which one to pick. `ls` reads each capsule's manifest — not its entries — and prints
the capsule, the run, the recovery status and its cause, the event count and the size.

The tests worth reading are the ones about what `ls` refuses to do: it does not recompute derived parts (a capsule with
unreadable internals must still list, because that is when someone runs `ls`), and a file that is not a capsule at all is
a **row with a reason**, not a traceback and not a silent omission.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cli import cmd_ls
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod


def _run(tmp_path: Path, run_id: str) -> list[Path]:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": run_id}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, b'{"m":"model"}', node_slot=1, run_slot=1)
    writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": run_id}), node_slot=0)
    writer.close()
    recovery.recover(str(data), writer.writer_uuid.hex())
    return sorted((data / "capsules").glob("*.bbx"))


def _ls(tmp_path: Path, *argv: str) -> tuple[int, str]:
    import contextlib
    import io
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cmd_ls.main(["--data", str(tmp_path / "data"), *argv])
    return code, buffer.getvalue()


def test_it_lists_what_a_run_produced(tmp_path: Path) -> None:
    paths = _run(tmp_path, "r_0123456789abcdef")
    assert paths
    code, out = _ls(tmp_path)
    assert code == 0
    header, *rows = out.splitlines()
    assert header.split()[:5] == ["CAPSULE", "RUN", "STATUS", "EVENTS", "SIZE"]
    assert len(rows) == len(paths)
    for line in rows:
        assert "recovered/PROCESS_DEATH" in line
        assert "r_0123456789abcdef" in line
        assert any(path.stem in line for path in paths)
        assert "KiB" in line or " B" in line


def test_the_json_is_the_same_rows_for_tooling(tmp_path: Path) -> None:
    _run(tmp_path, "r_0123456789abcdef")
    code, out = _ls(tmp_path, "--json")
    assert code == 0
    payload = json.loads(out)
    assert Path(payload["data_dir"]).is_absolute()
    assert payload["capsules"]
    row = payload["capsules"][0]
    assert row["readable"] is True
    assert row["status"] == "recovered" and row["status_cause"] == "PROCESS_DEATH"
    assert row["run_id"] == "r_0123456789abcdef"
    assert isinstance(row["events"], int) and row["events"] >= 1
    assert row["root_ok"] is True


def test_limit_and_newest_first(tmp_path: Path) -> None:
    _run(tmp_path, "r_0000000000000001")
    import os
    import time
    time.sleep(0.01)
    _run(tmp_path, "r_0000000000000002")
    code, out = _ls(tmp_path, "--json", "--limit", "1")
    assert code == 0
    assert len(json.loads(out)["capsules"]) == 1
    first = cmd_ls.capsules_in(str(tmp_path / "data"))[0]
    assert os.path.getmtime(first) >= max(os.path.getmtime(path) for path in cmd_ls.capsules_in(str(tmp_path / "data")))


def test_a_broken_file_is_a_row_with_a_reason(tmp_path: Path) -> None:
    _run(tmp_path, "r_0123456789abcdef")
    broken = tmp_path / "data" / "capsules" / "broken.bbx"
    broken.write_bytes(b"this is not a zip archive")
    code, out = _ls(tmp_path)
    assert code == 0                              # an unreadable capsule is information, not a command failure
    assert "unreadable" in out and "broken.bbx" in out
    assert "need attention" in out
    row = cmd_ls.describe(str(broken))
    assert row["readable"] is False and row["error"]


def test_an_empty_directory_says_how_to_make_one(tmp_path: Path) -> None:
    code, out = _ls(tmp_path)
    assert code == 0
    assert "no capsules" in out and "blackbox run" in out


def test_a_data_path_that_is_a_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "data").write_text("not a directory")
    code, out = _ls(tmp_path)
    assert code == 2 and "not a directory" in out


def test_it_never_recomputes_the_derived_parts(tmp_path: Path, monkeypatch) -> None:
    """`ls` reads manifests. If it needed the entries, a capsule with a damaged graph part could not be listed — and a
    damaged capsule is exactly when someone runs `ls`."""
    from blackbox.cap import derived

    _run(tmp_path, "r_0123456789abcdef")
    monkeypatch.setattr(derived, "frames_of", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no frames here")))
    code, out = _ls(tmp_path)
    assert code == 0 and "recovered/PROCESS_DEATH" in out

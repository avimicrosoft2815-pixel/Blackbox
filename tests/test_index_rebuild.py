"""I8 — the index is a pure function of its sources: rebuild it and compare canonical dumps (P5-06).

The acceptance the plan names is *"rebuild and compare canonical dumps over a 500-run corpus"*. The 500-run corpus here is
built the way a user's data directory is built: 500 real tapes, each sealed by the real recovery path, each capsule read
back by the real capsule reader. Nothing is hand-built, so a difference between two dumps would be a difference the index
made, not one the fixture made.

Why this is worth a 30-second test: the index is the only thing that answers "what have I recorded" without reading every
capsule, and it is derived — so it can be *wrong* in a way the capsules never are. If a rebuild produced even one row that
an incremental build did not, then either the incremental path is stale or the rebuild is not a function of the sources,
and every later claim about "the index says X" inherits the doubt. The dump deliberately excludes `index_meta`, which
holds `built_at_ns`: two builds a second apart differ there and must still agree here.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import blackbox.index.build as index_build
from blackbox import constants as C
from blackbox.cli import cmd_index
from blackbox.codec import cbor
from blackbox.index import schema
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter

RUNS = 500


def _tape(data_dir: Path, *, run_id: str = "corpus", frames: int = 3) -> str:
    """Write one small tape and return its writer uuid. Frames carry a fixed wall clock so nothing is host-timed."""
    writer = TapeWriter(str(data_dir), segment_bytes=C.MIN_SEGMENT_BYTES)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    assert writer.open() is writer
    try:
        # Payloads are CBOR, the way the launcher and the capture seams write them — a JSON payload still records, but
        # every reader then sees an empty body, and the declared run id would not be seen at seal time either.
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "source": "corpus"}), priority=0)
        writer.append(C.KIND_NODE_OPEN, cbor.encode({"name": "step", "kind": "step"}),
                      node_slot=1, priority=0, run_slot=1)
        for index in range(frames):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"model": "m", "n": index}),
                          node_slot=1, priority=0, run_slot=1,
                          wall_ns=1_700_000_000_000_000_000 + index * 1_000_000)
        writer.append(C.KIND_NODE_CLOSE, cbor.encode({"status": "ok"}), node_slot=1, priority=0, run_slot=1)
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.publish()
    finally:
        writer.close()
    return writer.writer_uuid.hex()


def _dump_lines(data_dir: Path) -> list[dict[str, object]]:
    """The canonical dump as rows. It is JSON Lines — one object per line — not one JSON array."""
    return [json.loads(line) for line in index_build.canonical_dump(data_dir).decode().splitlines()]


def _seal_everything(data_dir: Path) -> int:
    sealed = 0
    for tape in sorted(Path(data_dir, "tape").iterdir()):
        recovery.recover(str(data_dir), tape.name)
        sealed += 1
    return sealed


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """500 recorded runs, each sealed to a capsule by the recovery path a real `blackbox run` uses.

    Each tape is written and sealed in a scratch directory of its own and its capsule is moved into the corpus. That is
    not a shortcut around recovery — the same `recovery.recover` seals the same tape — it is a way to avoid a quadratic:
    sealing a tape in a directory that already holds N capsules makes the seal-time link resolver read all N of them, so
    one directory with 500 runs took 144 s to build and 500 one-run directories take about 25 s. The corpus is the same
    500 capsules either way, and `test_two_tapes_of_one_run_is_two_artifacts_and_one_run_row` below still exercises the
    multi-tape-in-one-directory path.
    """
    root = tmp_path_factory.mktemp("i8-corpus")
    data_dir = root / "corpus"
    (data_dir / "capsules").mkdir(parents=True)
    for run in range(RUNS):
        scratch = root / f"run-{run:03d}"
        scratch.mkdir()
        _tape(scratch, run_id=f"r_{run:016x}")
        assert _seal_everything(scratch) == 1
        (capsule,) = list(Path(scratch, "capsules").glob("*.bbx"))
        capsule.rename(data_dir / "capsules" / capsule.name)
    assert len(list(Path(data_dir, "capsules").glob("*.bbx"))) == RUNS
    return data_dir


def test_a_500_run_rebuild_reproduces_the_incremental_index(corpus: Path) -> None:
    """The acceptance line itself: ingest 500 capsules one at a time, then rebuild from nothing and compare."""
    started = time.perf_counter()
    incremental = index_build.build(corpus)
    incremental_ms = (time.perf_counter() - started) * 1000.0
    incremental_dump = index_build.canonical_dump(corpus)
    incremental_hash = index_build.dump_hash(corpus)

    started = time.perf_counter()
    rebuilt = index_build.rebuild(corpus)
    rebuilt_ms = (time.perf_counter() - started) * 1000.0
    rebuilt_hash = index_build.dump_hash(corpus)

    assert incremental["capsules_seen"] == RUNS and rebuilt["capsules_seen"] == RUNS
    assert rebuilt["mode"] == "rebuild"
    assert rebuilt["counts"]["run"] == RUNS, rebuilt["counts"]
    assert rebuilt["counts"]["node"] == RUNS
    # Nine frames per run: the seven this fixture appends, plus the writer's own SEG_END and CHAIN frames. The index
    # counts frames, not "interesting" frames, which is why the number comes from the capsule rather than from the test.
    assert rebuilt["counts"]["event"] == RUNS * 9, rebuilt["counts"]
    assert rebuilt["counts"]["artifact"] == RUNS
    assert incremental_hash == rebuilt_hash, (
        f"the rebuild is not a pure function of the capsules: incremental {incremental_hash} vs rebuilt {rebuilt_hash} "
        f"(incremental adds {incremental['rows_added']} rows in {incremental_ms:.0f} ms, rebuild "
        f"{rebuilt['rows_added']} in {rebuilt_ms:.0f} ms)")
    assert index_build.canonical_dump(corpus) == incremental_dump, "the dump's bytes, not only its hash, agree"
    assert incremental["unreadable"] == [] and rebuilt["unreadable"] == []
    assert incremental["disagreements"] == [] and rebuilt["disagreements"] == []
    # A rebuild of a 500-run corpus has to be a sensible amount of work, not a hang.
    assert rebuilt_ms < 120_000, f"rebuild took {rebuilt_ms:.0f} ms for {RUNS} capsules"


def test_two_rebuilds_of_an_unchanged_corpus_are_the_same_bytes(corpus: Path) -> None:
    first = index_build.rebuild(corpus)
    hash_a = index_build.dump_hash(corpus)
    time.sleep(0.01)                       # `built_at_ns` moves; the dump must not
    second = index_build.rebuild(corpus)
    assert second["rows_added"] == first["rows_added"]
    assert index_build.dump_hash(corpus) == hash_a


def test_re_ingesting_the_same_capsules_adds_no_rows(corpus: Path) -> None:
    index_build.rebuild(corpus)
    before = index_build.dump_hash(corpus)
    again = index_build.build(corpus)      # incremental over an index that already holds every capsule
    assert again["rows_added"] == 0, again
    assert index_build.dump_hash(corpus) == before


def test_a_new_capsule_changes_only_its_own_rows(tmp_path: Path) -> None:
    """Incremental ingest must be additive: one more capsule, one more run, the rest untouched."""
    for run in range(3):
        _tape(tmp_path, run_id=f"run-{run}")
    _seal_everything(tmp_path)
    index_build.rebuild(tmp_path)
    before = _dump_lines(tmp_path)
    _tape(tmp_path, run_id="run-late")
    _seal_everything(tmp_path)
    added = index_build.build(tmp_path)
    assert added["rows_added"] > 0
    after = _dump_lines(tmp_path)

    assert len(after) == len(before) + 1 + 1 + 9 + 1, "one run, one node, nine frames, one artifact"
    assert index_build.dump_hash(tmp_path) == index_build.dump_hash(tmp_path)


def test_two_tapes_of_one_run_are_two_artifacts_and_one_run_row(tmp_path: Path) -> None:
    """A run can have several tapes (SDK tape + proxy tape). The run row is one of them; both capsules are artifacts."""
    shared = "r_00000000000000aa"          # a well-formed run id: a tape that names anything else falls back to its key
    _tape(tmp_path, run_id=shared)
    _tape(tmp_path, run_id=shared)
    _seal_everything(tmp_path)
    report = index_build.rebuild(tmp_path)
    assert report["counts"]["run"] == 1, report["counts"]
    assert report["counts"]["artifact"] == 2
    dump = _dump_lines(tmp_path)
    run_rows = [line for line in dump if "run" in line]
    assert len(run_rows) == 1
    first = run_rows[0]["run"]
    # The row must be the first capsule in sorted order, not whichever tape was recovered last.
    assert first["capsule"] == sorted(p.name for p in Path(tmp_path, "capsules").glob("*.bbx"))[0]


def test_an_unreadable_capsule_is_reported_not_fatal(tmp_path: Path) -> None:
    _tape(tmp_path, run_id="good")
    _seal_everything(tmp_path)
    Path(tmp_path, "capsules", "broken.bbx").write_bytes(b"not a capsule")
    report = index_build.rebuild(tmp_path)
    assert report["counts"]["run"] == 1, "the good capsule is still indexed"
    assert len(report["unreadable"]) == 1 and report["unreadable"][0]["capsule"] == "broken.bbx", report["unreadable"]
    assert "BBX-" in report["unreadable"][0]["error"] or "Error" in report["unreadable"][0]["error"]


def test_the_dump_has_no_absolute_path_and_no_timing(tmp_path: Path) -> None:
    _tape(tmp_path, run_id="paths")
    _seal_everything(tmp_path)
    index_build.rebuild(tmp_path)
    text = index_build.canonical_dump(tmp_path).decode()
    assert str(tmp_path) not in text, "a dump that carries the data directory cannot be compared across machines"
    assert "built_at_ns" not in text and "last_rebuild_ns" not in text
    meta = schema.meta(schema.connect(tmp_path))
    assert meta["schema_version"] == str(schema.SCHEMA_VERSION)
    assert meta["schema_strict"] == ("1" if schema.supports_strict() else "0")


def test_the_index_never_deletes_a_capsule(tmp_path: Path) -> None:
    _tape(tmp_path, run_id="keep")
    _seal_everything(tmp_path)
    before = {p.name: p.stat().st_size for p in Path(tmp_path, "capsules").glob("*.bbx")}
    index_build.rebuild(tmp_path)
    index_build.build(tmp_path)
    index_build.rebuild(tmp_path)
    after = {p.name: p.stat().st_size for p in Path(tmp_path, "capsules").glob("*.bbx")}
    assert before == after


def test_the_command_reports_what_it_did_and_refuses_a_missing_directory(tmp_path: Path, capsys) -> None:
    _tape(tmp_path, run_id="cli")
    _seal_everything(tmp_path)
    assert cmd_index.main(["--data", str(tmp_path), "--rebuild", "--dump", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "rebuild" and report["counts"]["run"] == 1
    assert len(report["dump_sha256"]) == 64 and report["dump_rows"] == 12
    assert cmd_index.main(["--data", str(tmp_path / "nope")]) == 2

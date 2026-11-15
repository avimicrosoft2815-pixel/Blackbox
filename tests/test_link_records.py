"""P3B-04's second half: the `LINK` record on the wire, and where it is allowed to be written.

The resolver (`tests/test_links.py`) decides *what* a link is. This file is about the half either side of it — the
bytes that carry the decision and the place the decision is stored — and it is written around three claims:

* **A payload is input, not a promise.** `links.parse` checks the fields the invariants rest on, and refuses an
  `inferred` link claiming confidence 1.0: the class exists to say nobody asserted it.
* **The record lands beside the tape, not in it.** A tape's bytes are the evidence a capsule hashes, so a resolver that
  appended to the tape would be editing what it describes. The capsule gets `links.json`; the tape is byte-identical
  before and after, and carries no `LINK` frame.
* **A declared link is a statement, and statements are recorded once.** `blackbox link a b` writes the pair into both
  data directories (a declaration only one side remembers is one the other side's evidence contradicts), is idempotent,
  and refuses a directory whose tape does not name a run rather than linking `None` to `None`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.cli import link as link_cli
from blackbox.codec import cbor
from blackbox.errors import BlackBoxError
from blackbox.rec import correlate, links, recovery
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

SDK, PROXY = 1, 2


def _writer(tmp_path: Path, *, vantage: int = SDK, run: str | None = None,
            linked_run: str | None = None, fingerprint: str = "fp-a") -> TapeWriter:
    writer = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES, vantage=vantage)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    writer.open()
    if run is not None:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": run}), priority=0)
    if fingerprint:
        writer.append(C.KIND_MODEL_REQUEST,
                      cbor.encode({"request_fp": fingerprint, "linked_run": linked_run}), node_slot=1, priority=0)
    writer.append(C.KIND_RUN_END, cbor.encode({"run_id": run or ""}), priority=0)
    writer.publish()
    writer.close()
    return writer


def _link(**overrides: object) -> links.Link:
    fields: dict[str, object] = {"klass": links.EXACT, "left_writer": "writer-proxy", "right_writer": "writer-sdk",
                                 "left_run": "r_1", "right_run": "r_1", "confidence": 1.0,
                                 "evidence": {"rule": "same run id"}}
    fields.update(overrides)
    return links.Link(**fields)          # type: ignore[arg-type]


def _frame(kind: int, payload: bytes, seq: int = 1) -> dict[str, object]:
    return {"seq": seq, "kind": kind, "payload": payload, "wall_ns": 1, "node_slot": 1}


# ── the kind ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_link_is_a_graph_kind_at_p2_priority_and_no_existing_kind_moved() -> None:
    assert 0x10 <= C.KIND_LINK <= 0x1F, "LINK belongs to the plan's graph range (§8.4)"
    assert C.KIND_NAMES[C.KIND_LINK] == "LINK"
    assert C.KIND_FROM_NAME["LINK"] == C.KIND_LINK
    # Append-only means append-only: a renumbering is caught here rather than in a customer's tape.
    published = {C.KIND_SEG_END: 1, C.KIND_CHAIN: 2, C.KIND_INTERN: 3, C.KIND_GAP: 4, C.KIND_NODE_OPEN: 16,
                 C.KIND_NODE_CLOSE: 17, C.KIND_EDGE: 18, C.KIND_MODEL_REQUEST: 32, C.KIND_RUN_END: 81,
                 C.KIND_MARK: 112}
    assert all(kind == number for kind, number in published.items())
    assert C.KIND_LINK not in C.PRIORITY_KINDS, ("§11.10: the resolver writes links at recovery/seal time, "
                                                 "not on the hot path")


# ── the payload ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_link_round_trips_through_the_payload_a_writer_would_append() -> None:
    record = _link(klass=links.INFERRED, confidence=0.75,
                   evidence={"rule": "time window and content fingerprint", "delta_ns": 120})
    frames = links.frames_for([record])
    assert [kind for kind, _ in frames] == [C.KIND_LINK]
    read_back = links.from_frames([_frame(kind, payload) for kind, payload in frames])
    assert read_back == [record]
    assert read_back[0].as_dict() == record.as_dict()


def test_an_inference_may_not_claim_certainty_on_its_way_back_off_a_tape() -> None:
    payload = cbor.decode_exact(cbor.encode({**_link(klass=links.INFERRED, confidence=1.0).as_dict()}))
    with pytest.raises(BlackBoxError) as caught:
        links.parse(payload)
    assert caught.value.code == "BBX-E155" and "1.0" in caught.value.detail
    assert links.parse({**_link(klass=links.INFERRED, confidence=0.9).as_dict()}).klass == links.INFERRED


@pytest.mark.parametrize("damage", ["unknown-class", "missing-field", "self-link", "confidence-over-one"])
def test_a_payload_that_breaks_an_invariant_is_refused_by_its_reason(damage: str) -> None:
    body = _link().as_dict()
    if damage == "unknown-class":
        body["class"] = "probably"
        needle = "unknown link class"
    elif damage == "missing-field":
        del body["evidence"]
        needle = "missing evidence"
    elif damage == "self-link":
        body["right_writer"] = body["left_writer"]
        needle = "itself"
    else:
        body["confidence"] = 1.5
        needle = "outside [0, 1]"
    with pytest.raises(BlackBoxError) as caught:
        links.parse(body)
    assert caught.value.code == "BBX-E155"
    assert needle in caught.value.detail


def test_a_frame_that_is_not_cbor_is_an_error_and_other_kinds_are_ignored() -> None:
    good = links.frames_for([_link()])
    read = links.from_frames([_frame(C.KIND_MODEL_REQUEST, b'{"not":"a link"}'),
                              _frame(C.KIND_LINK, good[0][1])])
    assert len(read) == 1
    with pytest.raises(BlackBoxError) as caught:
        links.from_frames([_frame(C.KIND_LINK, b"\xff\xff\xff")])
    assert caught.value.code == "BBX-E155"


# ── tapes in, views out ──────────────────────────────────────────────────────────────────────────────────────────
def test_the_proxy_tape_becomes_a_view_that_names_the_run_it_carried(tmp_path: Path) -> None:
    proxy = _writer(tmp_path, vantage=PROXY, linked_run="r_1")
    scan = scanner.scan(str(tmp_path / "data"), proxy.writer_uuid.hex())
    view = correlate.view_of(scan)
    assert view.vantage == "proxy" and view.run_id == "r_1"
    assert [call.fingerprint for call in view.calls] == ["fp-a"]


def test_a_proxy_tape_carrying_two_runs_gets_no_exact_link_and_says_why(tmp_path: Path) -> None:
    """One proxy, two SDK runs through it: the tape is not *a* run, so it claims none — and nothing is guessed."""
    _writer(tmp_path, vantage=SDK, run="r_1")
    proxy = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES, vantage=PROXY)
    proxy.batch_bytes = 0
    proxy.batch_ms = 0.0
    proxy.open()
    for run, fingerprint in (("r_1", "fp-a"), ("r_2", "fp-b")):
        proxy.append(C.KIND_MODEL_REQUEST,
                     cbor.encode({"request_fp": fingerprint, "linked_run": run}), priority=0)
    proxy.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
    proxy.publish()
    proxy.close()
    found = correlate.resolve_data_dir(str(tmp_path / "data"))
    assert [link.klass for link in found] == [links.INFERRED], (
        "two runs through one proxy is not *a* run id, so the tape may only be linked by inference — and the "
        "evidence has to say that is what happened")
    assert found[0].evidence["why_not_exact"] == "neither tape names the other's run id"
    assert found[0].confidence < 1.0
    assert correlate.declared_pairs(str(tmp_path / "data")) == []


# ── the resolver over real tapes ─────────────────────────────────────────────────────────────────────────────────
def test_an_exact_link_is_found_between_two_real_tapes_and_points_from_the_proxy(tmp_path: Path) -> None:
    sdk = _writer(tmp_path, vantage=SDK, run="r_1")
    proxy = _writer(tmp_path, vantage=PROXY, linked_run="r_1")
    found = correlate.resolve_data_dir(str(tmp_path / "data"))
    assert len(found) == 1
    link = found[0]
    assert link.klass == links.EXACT and link.confidence == 1.0
    assert (link.left_writer, link.right_writer) == (proxy.writer_uuid.hex(), sdk.writer_uuid.hex())
    assert link.evidence["carried_by"] == "x-blackbox-run"


# ── the artifact, and the tape it must not touch ─────────────────────────────────────────────────────────────────
def _segment_bytes(data_dir: Path) -> list[bytes]:
    return [path.read_bytes() for path in sorted((data_dir / "tape").glob("*/0*.seg"))]


def test_the_capsule_carries_links_json_and_the_tapes_are_byte_identical_afterwards(tmp_path: Path) -> None:
    _writer(tmp_path, vantage=SDK, run="r_1")
    proxy = _writer(tmp_path, vantage=PROXY, linked_run="r_1")
    data = tmp_path / "data"
    before = _segment_bytes(data)
    result = recovery.recover(str(data), proxy.writer_uuid.hex())
    after = _segment_bytes(data)
    assert before == after, "recovery may describe a tape, never edit it"
    capsule = cap.read_capsule(result.capsule_path)
    document = json.loads(capsule.entries["links.json"].decode("utf-8"))
    assert document["summary"] == {"exact": 1, "declared": 0, "inferred": 0}
    assert document["links"][0]["class"] == "exact"
    # And the tape itself holds no LINK frame: the resolver is not a producer on somebody else's tape.
    scan = scanner.scan(str(data), proxy.writer_uuid.hex())
    assert links.from_frames(scan.frames) == []


def test_a_capsule_with_nothing_to_link_says_so_instead_of_omitting_the_file(tmp_path: Path) -> None:
    alone = _writer(tmp_path, vantage=SDK, run="r_solo")
    data = tmp_path / "data"
    first = recovery.recover(str(data), alone.writer_uuid.hex())
    capsule = cap.read_capsule(first.capsule_path)
    document = json.loads(capsule.entries["links.json"].decode("utf-8"))
    assert document["links"] == []
    assert document["summary"] == {"exact": 0, "declared": 0, "inferred": 0}
    second = correlate.for_capsule(str(data), alone.writer_uuid.hex())
    assert second == capsule.entries["links.json"], "the same tapes must produce the same bytes, twice"


# ── the command ───────────────────────────────────────────────────────────────────────────────────────────────────
def _two_dirs(tmp_path: Path) -> tuple[Path, Path]:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _writer(first, vantage=SDK, run="r_a")
    _writer(second, vantage=SDK, run="r_b")
    return first / "data", second / "data"


def test_blackbox_link_records_one_declared_pair_in_both_directories_and_is_idempotent(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    left, right = _two_dirs(tmp_path)
    assert link_cli.main([str(left), str(right)]) == 0
    capsys.readouterr()                       # the first run's document is read below, not accumulated
    assert link_cli.main([str(left), str(right)]) == 0
    for directory in (left, right):
        pairs = correlate.declared_pairs(str(directory))
        assert pairs == [("r_a", "r_b")], f"{directory} must remember the assertion exactly once"
    body = json.loads(capsys.readouterr().out)
    assert body["declared"]["by"] == "user"
    assert body["summary"] == {"exact": 0, "declared": 1, "inferred": 0}
    assert body["links"][0]["class"] == "declared"
    assert body["links"][0]["evidence"]["asserted_by"] == "user"


def test_blackbox_link_refuses_a_directory_whose_tape_names_no_run(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    left, _ = _two_dirs(tmp_path)
    anonymous = tmp_path / "c"
    anonymous.mkdir()
    _writer(anonymous, vantage=PROXY, linked_run=None, fingerprint="")
    assert link_cli.main([str(left), str(anonymous / "data")]) == 2
    assert "no tape names its run" in capsys.readouterr().err
    assert correlate.declared_pairs(str(left)) == [], "a refused link leaves no record behind"


def test_a_declared_pair_beats_an_inference_and_keeps_its_own_class(tmp_path: Path) -> None:
    left, right = _two_dirs(tmp_path)
    correlate.record_declared(str(left), "r_a", "r_b")
    found = correlate.resolve_across([str(left), str(right)])
    assert [link.klass for link in found] == [links.DECLARED]
    assert found[0].confidence == 1.0 and found[0].evidence["asserted_by"] == "user"

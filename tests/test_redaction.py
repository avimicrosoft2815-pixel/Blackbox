"""P3-11: redaction before persistence, proved against 60 secrets and every chunk boundary that can split them.

The claim under test is not "the redactor replaces what it matches". It is the one a user needs: **no recognisable
piece of any secret in the corpus reaches the tape, whatever the chunk boundaries are**. So the tests feed the
corpus through the redactor at every split offset — the network chooses where a read ends, and a secret cut in half
at a boundary is the case a per-chunk regex cannot see — and check for the whole secret, its head and its tail.

Three modes are covered because they are three different promises: `full` (a placeholder), `hash` (a stable
pseudonym, so the same key is recognisable across a run without being recoverable), and `off` (the user chose to
keep content; the tests assert it really is off rather than quietly redacting). And the end-to-end test runs the
corpus through a real seam, a real sink and a real tape, then greps the tape: the unit tests can all pass while the
sink writes something else.
"""
from __future__ import annotations

import random
import re

import pytest
from _secrets import SECRETS, corpus, leaked, splits

from blackbox import api
from blackbox import constants as C
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.redact import MODES, WINDOW, Redactor
from blackbox.tape import scanner

SECRETS_BYTES = corpus()


# ── the corpus itself ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_corpus_has_sixty_secrets_of_ten_kinds_and_none_of_them_is_real():
    assert len(SECRETS) == 60
    assert len({kind for kind, _ in SECRETS}) == 10
    assert len({secret for _kind, secret in SECRETS}) == 60, "two identical secrets would weaken the leak test"
    assert all("example" in s or s.startswith(("sk-", "AKIA", "ghp_", "xoxb-", "eyJ", "Bearer", "api_key", "-----"))
               for _kind, s in SECRETS)


@pytest.mark.parametrize("kind,secret", SECRETS, ids=[f"{kind}-{i}" for i, (kind, _) in enumerate(SECRETS)])
def test_every_kind_is_recognised_alone(kind, secret):
    redactor = Redactor("full")
    out = redactor.feed(secret.encode()) + redactor.flush()
    assert not leaked(secret.encode(), out), f"{kind} was not redacted"
    assert kind in redactor.counts, f"{kind} matched as {list(redactor.counts)} instead of its own kind"


# ── the boundary torture ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("index", range(len(SECRETS)))
def test_no_split_of_a_secret_leaks_a_piece_of_it(index):
    """Every offset that divides the secret, one test per secret: 60 × their lengths splits, all of them."""
    _kind, secret = SECRETS[index]
    raw = secret.encode()
    for offset in splits(raw):
        redactor = Redactor("full")
        out = redactor.feed(b"before " + raw[:offset]) + redactor.feed(raw[offset:] + b" after")
        out += redactor.flush()
        assert not leaked(raw, out), f"split at {offset} leaked a piece of {raw[:8]!r}…: {out!r}"


def test_random_chunk_sizes_never_leak_anything_from_the_whole_corpus():
    """The realistic case: one stream carrying all 60 secrets, cut at sizes nobody chose."""
    body = b" | ".join([b"line", *SECRETS_BYTES, b"end"])
    rng = random.Random(20261117)  # noqa: S311 - a test choosing chunk sizes, not a key
    for _round in range(20):
        redactor = Redactor("full")
        out = bytearray()
        cursor = 0
        while cursor < len(body):
            size = rng.choice([1, 2, 3, 7, 13, 64, 512, 4096, 8192])
            out += redactor.feed(body[cursor:cursor + size])
            cursor += size
        out += redactor.flush()
        for raw in SECRETS_BYTES:
            assert not leaked(raw, bytes(out)), f"a secret survived a random chunking: {raw[:8]!r}…"
        assert sum(redactor.counts.values()) == 60, redactor.counts


def test_ordinary_text_and_structure_survive_untouched():
    """A redactor that mangles the trace is a redactor nobody will leave switched on."""
    body = (b'data: {"choices":[{"delta":{"content":"The answer is 42."}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n'
            b"data: [DONE]\n\n")
    redactor = Redactor("full")
    out = redactor.feed(body) + redactor.flush()
    assert out == body
    assert redactor.counts == {}


def test_a_secret_that_arrives_one_byte_at_a_time_is_still_caught():
    raw = SECRETS_BYTES[0]
    redactor = Redactor("full")
    out = b"".join(redactor.feed(raw[i:i + 1]) for i in range(len(raw))) + redactor.flush()
    assert not leaked(raw, out)
    assert redactor.counts


# ── the three modes ─────────────────────────────────────────────────────────────────────────────────────────────


def test_hash_mode_is_a_stable_pseudonym_and_not_the_secret():
    secret = SECRETS[0][1].encode()
    first = Redactor("hash")
    second = Redactor("hash")
    a = first.feed(secret) + first.flush()
    b = second.feed(secret) + second.flush()
    assert a == b, "the same secret must produce the same placeholder, or a trace cannot be correlated"
    assert re.fullmatch(rb"\[REDACTED:[a-z_]+:[0-9a-f]{8}\]", a), a
    assert not leaked(secret, a)

    other = Redactor("hash")
    c = other.feed(SECRETS[1][1].encode()) + other.flush()
    assert a != c, "two different secrets must not share a placeholder"


def test_off_mode_really_is_off():
    secret = SECRETS[0][1].encode()
    redactor = Redactor("off")
    out = redactor.feed(secret) + redactor.flush()
    assert out == secret
    assert redactor.counts == {}
    assert redactor.as_dict()["mode"] == "off"


def test_an_unknown_mode_is_refused_rather_than_silently_treated_as_one_of_the_known_ones():
    with pytest.raises(ValueError, match="unknown redaction mode"):
        Redactor("aggressive")
    assert set(MODES) == {"full", "hash", "off"}


def test_the_redactor_holds_no_content_beyond_its_window():
    """It keeps a tail of the stream, never the stream: bounded buffer, counters, and nothing after `flush`."""
    redactor = Redactor("full")
    for raw in SECRETS_BYTES:
        out = redactor.feed(raw)
        assert len(redactor._buf) <= WINDOW + max(len(r) for r in SECRETS_BYTES) + 1
        for other in SECRETS_BYTES:                       # nothing already released may still be held
            assert other not in out or not leaked(other, out)
    redactor.flush()
    assert redactor._buf == b"", "after the stream ends the buffer must be empty, not merely small"
    state = repr(redactor.__dict__)
    for raw in SECRETS_BYTES:
        assert raw.decode() not in state, "the redactor is holding content in its own state"


def test_binary_payloads_do_not_break_it():
    """Multimodal bodies are bytes too. A secret inside binary noise is still a secret."""
    secret = SECRETS[0][1].encode()
    blob = bytes(range(256)) * 4 + secret + bytes(range(255, 0, -1)) * 4
    redactor = Redactor("full")
    out = redactor.feed(blob) + redactor.flush()
    assert not leaked(secret, out)
    assert out.startswith(bytes(range(256)) * 4), "the binary content before the secret must be preserved"


# ── end to end: through the sink onto a real tape ───────────────────────────────────────────────────────────────


def test_no_secret_from_the_corpus_reaches_a_real_tape(tmp_path):
    """The unit tests can all pass while the sink writes something else. This one greps the tape."""
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    redactor = Redactor("full")
    sink = TapeSink(api.emit, coalesce_bytes=256, redactor=redactor, content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        body = b" | ".join(SECRETS_BYTES)
        for start in range(0, len(body), 97):                 # 97: a size that cuts secrets in the middle
            recorder.receive(call, body[start:start + 97])
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    uuid = next((tmp_path / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path), uuid)
    tape_bytes = b"".join(frame["payload"] for frame in scan.frames
                          if frame["kind"] in (C.KIND_MODEL_CHUNK, C.KIND_MODEL_RESPONSE))
    for raw in SECRETS_BYTES:
        assert not leaked(raw, tape_bytes), f"a secret reached the tape: {raw[:8]!r}…"
    assert redactor.counts, "nothing was redacted, so this test would pass on an empty stream"
    assert sum(redactor.counts.values()) == 60

    summary_frame = next(frame for frame in scan.frames if frame["kind"] == C.KIND_MODEL_RESPONSE)
    import json

    summary = json.loads(summary_frame["payload"])
    assert summary["redaction"]["counts"] == redactor.counts
    assert summary["redaction"]["bytes_in"] > summary["redaction"]["bytes_out"]


def test_the_response_frame_says_what_was_removed_not_what_it_was(tmp_path):
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    redactor = Redactor("hash")
    sink = TapeSink(api.emit, redactor=redactor, content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, SECRETS_BYTES[0])
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    uuid = next((tmp_path / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path), uuid)
    whole = b"".join(frame["payload"] for frame in scan.frames)
    assert not leaked(SECRETS_BYTES[0], whole)
    assert b"redaction" in whole


# ── the seam path: the redactor must not change what the application reads ────────────────────────────────────


def test_redaction_happens_after_the_application_has_its_bytes():
    """The redactor is between the seam and the tape, never between the provider and the application."""
    from blackbox.cap_sdk.seam_httpx_hooks import install

    httpx = pytest.importorskip("httpx")
    api.install(data_dir="/tmp/blackbox-redaction-unused", publish_ms=0.0)
    redactor = Redactor("full")
    sink = TapeSink(api.emit, redactor=redactor, content="full")
    recorder = SinkRecorder(sink)
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=4, chunk_text="key sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA ")) as provider, \
                httpx.Client(base_url=provider.base_url, timeout=10) as client, \
                client.stream("POST", "/v1/chat/completions",
                              json={"model": "m", "stream": True}) as response:
            seen = b"".join(response.iter_bytes())
    finally:
        handle.uninstall()
        api.uninstall()

    assert b"sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" in seen, "redaction changed what the application received"
    assert redactor.counts.get("openai", 0) >= 1


# ── what the default policy costs a stream, and what it must not cost ──────────────────────────────────────────


def test_ordinary_text_is_released_on_the_read_that_brought_it():
    """Why there is no fixed 4 KiB tail: that tail is what a `kill -9` would cost, and it is almost never needed.

    A credential in this corpus is one token. Text that ends a token — a newline, a space, a brace — cannot be the
    inside of one, so it is released immediately and the tape is current to the last complete token.
    """
    redactor = Redactor("full")
    line = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    assert redactor.feed(line) == line
    assert redactor._buf == b"", "a held tail is a tail a hard kill loses"


def test_a_three_byte_read_is_an_unfinished_token_and_is_held():
    """The cost is named rather than hidden: `abc` could be the first three characters of an address or a key."""
    redactor = Redactor("full")
    assert redactor.feed(b"abc") == b""
    assert redactor.feed(b"\n") == b"abc\n"


def test_the_default_policy_comes_from_configuration_and_a_typo_is_not_a_decision(monkeypatch):
    monkeypatch.delenv("BLACKBOX_REDACTION", raising=False)
    assert Redactor.from_config().mode == "full"
    monkeypatch.setenv("BLACKBOX_REDACTION", "hash")
    assert Redactor.from_config().mode == "hash"
    monkeypatch.setenv("BLACKBOX_REDACTION", "off")
    assert Redactor.from_config().mode == "off"
    monkeypatch.setenv("BLACKBOX_REDACTION", "of")                  # a typo
    assert Redactor.from_config().mode == "full", "a misspelt mode must not be read as \"off\""
    monkeypatch.setenv("BLACKBOX_REDACTION_KINDS", "email, openai")
    assert Redactor.from_config().kinds == ("email", "openai")


def test_a_sink_that_was_never_told_to_redact_redacts_anyway():
    """The default is the policy, not the absence of one: `TapeSink(emit, content="full")` with no argument still redacts."""
    frames: list[tuple[int, bytes, dict]] = []
    sink = TapeSink(lambda kind, payload, **fields: frames.append((kind, payload, fields)), content="full")
    recorder = SinkRecorder(sink)
    call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
    recorder.receive(call, b"the key is " + SECRETS_BYTES[1] + b" and that is all\n")
    recorder.close_call(call, status=200)

    stored = b"".join(payload for kind, payload, _ in frames if kind == C.KIND_MODEL_CHUNK)
    assert not leaked(SECRETS_BYTES[1], stored + b"".join(payload for kind, payload, _ in frames
                                                          if kind == C.KIND_MODEL_RESPONSE))
    assert b"the key is " in stored and b"and that is all" in stored, "redaction ate the sentence around the key"


def test_the_summary_counts_reads_and_stored_segments_separately():
    """`chunks_raw` is what the application read; `chunks_stored` is what the tape has. They differ as soon as
    redaction sits between them, and a reader must not be able to mistake one for the other."""
    import json

    frames: list[tuple[int, bytes, dict]] = []
    sink = TapeSink(lambda kind, payload, **fields: frames.append((kind, payload, fields)))
    recorder = SinkRecorder(sink)
    call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
    reads = [b"Bearer " + b"A" * 12, b"A" * 12 + b"\n", b"tail\n"]
    for read in reads:
        recorder.receive(call, read)
    recorder.close_call(call, status=200)

    summary = json.loads(next(payload for kind, payload, _ in frames if kind == C.KIND_MODEL_RESPONSE))
    assert summary["chunks_raw"] == 3, "three reads went in"
    assert summary["chunks_stored"] == 2, "the first read was held, so two segments came out, not three"
    assert summary["bytes_received"] == sum(len(read) for read in reads), "what the application read is unchanged"
    assert summary["bytes_stored"] == len(b"[REDACTED:bearer]\n") + len(b"tail\n")
    assert summary["redaction"]["counts"] == {"bearer": 1}


def test_a_megabyte_of_ordinary_sse_costs_less_than_the_call_it_records():
    """A capture layer that is slower than the model is a capture layer people turn off. This is a ceiling, not a
    benchmark: 1 MiB through the default policy in under a second, on the machine that runs the tests."""
    import time

    redactor = Redactor("full")
    line = b'data: {"choices":[{"delta":{"content":"lorem ipsum dolor sit amet"}}]}\n\n'
    block = line * (-(-1024 * 1024 // len(line)))
    started = time.perf_counter()
    for start in range(0, len(block), 8192):
        redactor.feed(block[start:start + 8192])
    redactor.flush()
    elapsed = time.perf_counter() - started
    assert len(block) >= 1024 * 1024
    assert elapsed < 1.0, f"{len(block)} bytes took {elapsed:.2f}s; redaction is the bottleneck"
    assert redactor.counts == {}


def test_a_redactor_that_breaks_fails_closed_and_says_so():
    """The architecture's C4, tested: an event is still recorded, its content is not, and the count is visible.

    The alternative — drop the segment and carry on — leaves a stream that is quietly shorter than the one the
    application saw, which is the kind of hole a support engineer spends a day finding.
    """
    import json

    class Broken:
        mode = "full"

        def feed(self, chunk: bytes) -> bytes:
            raise RuntimeError("redactor exploded")

        def flush(self) -> bytes:
            raise RuntimeError("redactor exploded")

        def as_dict(self) -> dict:
            return {"mode": "full", "counts": {}, "bytes_in": 0, "bytes_out": 0, "window": WINDOW}

    frames: list[tuple[int, bytes, dict]] = []
    sink = TapeSink(lambda kind, payload, **fields: frames.append((kind, payload, fields)), redactor=Broken(),
                    content="full")
    recorder = SinkRecorder(sink)
    call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
    secret = SECRETS_BYTES[0]
    recorder.receive(call, secret)
    recorder.close_call(call, status=200)

    stored = b"".join(payload for kind, payload, _ in frames if kind in (C.KIND_MODEL_CHUNK, C.KIND_MODEL_RESPONSE))
    assert not leaked(secret, stored), "a broken redactor let its input through"
    assert b"[REDACTION_FAILED]" in stored
    summary = json.loads(next(payload for kind, payload, _ in frames if kind == C.KIND_MODEL_RESPONSE))
    assert summary["redaction"]["failures"] == 2, "both the chunk and the tail failed"
    assert summary["bytes_received"] == len(secret), "the application's byte count is unchanged by a broken redactor"

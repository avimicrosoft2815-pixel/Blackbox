"""The per-machine redaction key (DV-013, §12.2 stage 7, D8).

`[REDACTED:openai:a1b2c3d4]` is only worth having if the four bytes cannot be turned back into the key. With a bare
`sha256` they can, for any secret short enough to enumerate — and a 12-character API key is short enough. So the
digest is an HMAC under a 32-byte key that lives in one file on one machine, and this file is the set of claims that
makes that sentence true rather than decorative:

* the key is created once, mode `0600`, and reused;
* two redactors with the same key agree; two with different keys do not;
* the pseudonym is the HMAC, not the digest;
* the key never appears in a capsule — only its id does;
* when the key cannot be had, the fallback **says** it is unkeyed, because a capsule that claims a keyed pseudonym it
  did not use is worse than one that was never keyed at all.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from blackbox import api
from blackbox import constants as C
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.keys import KEY_BYTES, load_or_create_key
from blackbox.redact import Redactor
from blackbox.tape import scanner

ROOT = Path(__file__).resolve().parents[1]
SECRET = b"sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX000"


def test_the_key_is_created_once_with_0600_and_reused(tmp_path, monkeypatch):
    path = tmp_path / "keys" / "redaction.key"
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(path))
    first = load_or_create_key()
    assert len(first) == KEY_BYTES
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"the key file is mode {mode:o}"
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert load_or_create_key() == first, "a second call must read the key it made, not make another"


def test_the_default_location_is_the_one_the_docs_name(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("BLACKBOX_KEY_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    load_or_create_key()
    assert (home / ".blackbox" / "keys" / "redaction.key").exists()


def test_the_pseudonym_is_the_hmac_and_not_the_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(tmp_path / "redaction.key"))
    redactor = Redactor("hash")
    assert redactor.keyed and redactor.key is not None
    out = redactor.feed(SECRET) + redactor.flush()
    expected = hmac.new(redactor.key, SECRET, hashlib.sha256).hexdigest()[:8].encode()
    assert out == b"[REDACTED:openai:" + expected + b"]"
    assert expected != hashlib.sha256(SECRET).hexdigest()[:8].encode(), "this is the digression DV-013 recorded"


def test_the_same_key_agrees_and_a_different_key_does_not(tmp_path, monkeypatch):
    one = tmp_path / "one.key"
    two = tmp_path / "two.key"
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(one))
    a = Redactor("hash")
    b = Redactor("hash")
    assert (a.feed(SECRET) + a.flush()) == (b.feed(SECRET) + b.flush()), "one key, one pseudonym"

    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(two))
    c = Redactor("hash")
    assert c.key_id != a.key_id
    assert (c.feed(SECRET) + c.flush()) != (a.feed(SECRET) + a.flush()), "a different key must not correlate"


def test_a_capsule_names_the_key_and_never_contains_it(tmp_path, monkeypatch):
    key_path = tmp_path / "redaction.key"
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(key_path))
    data_dir = tmp_path / "data"
    api.install(data_dir=str(data_dir), publish_ms=0.0)
    try:
        redactor = Redactor("hash")
        sink = TapeSink(api.emit, redactor=redactor, content="full")
        recorder = SinkRecorder(sink)
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, b"key " + SECRET + b" end\n")
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    key = key_path.read_bytes()
    uuid = next((data_dir / "tape").iterdir()).name
    scan = scanner.scan(str(data_dir), uuid)
    whole = b"".join(frame["payload"] for frame in scan.frames)
    summary = json.loads(next(f["payload"] for f in scan.frames if f["kind"] == C.KIND_MODEL_RESPONSE))

    assert SECRET not in whole
    assert key not in whole and key.hex().encode() not in whole, "the key is in the capsule"
    assert summary["redaction"]["keyed"] is True
    assert summary["redaction"]["key_id"] == hashlib.sha256(key).hexdigest()[:8]
    assert b"key_id" in whole, "a reader has to be able to see which key produced the pseudonym"


def test_an_unusable_key_falls_back_and_records_that_it_did(tmp_path, monkeypatch):
    """A read-only home must not stop the recorder — and must not let it claim a key it never used."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where a directory would have to be")
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(blocker / "redaction.key"))
    redactor = Redactor("hash")
    assert redactor.keyed is False and redactor.key_id is None
    out = redactor.feed(SECRET) + redactor.flush()
    assert out == b"[REDACTED:openai:" + hashlib.sha256(SECRET).hexdigest()[:8].encode() + b"]"
    assert redactor.as_dict()["keyed"] is False
    assert redactor.as_dict()["key_id"] is None


def test_modes_that_write_no_pseudonym_do_not_touch_a_key(tmp_path, monkeypatch):
    path = tmp_path / "redaction.key"
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(path))
    Redactor("full").feed(SECRET)
    Redactor("off").feed(SECRET)
    assert not path.exists(), "`full` and `off` have no use for a key, so creating one would be a lie"


def test_four_processes_starting_at_once_agree_on_one_key(tmp_path, monkeypatch):
    """`O_EXCL` plus a read-back: the loser of the race must read the winner's key, not make a second one."""
    path = tmp_path / "racy.key"
    program = (
        "import sys; sys.path.insert(0, {!r})\n"
        "from blackbox.keys import load_or_create_key\n"
        "print(load_or_create_key({!r}).hex())\n".format(str(ROOT / "src"), str(path))
    )
    procs = [subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")}) for _ in range(4)]
    outputs = {proc.communicate(timeout=60)[0].strip() for proc in procs}
    assert len(outputs) == 1, f"the processes disagreed on the key: {outputs}"
    assert len(path.read_bytes()) == KEY_BYTES


@pytest.mark.parametrize("mode", ["full", "off"])
def test_a_mode_that_is_not_hash_never_reports_a_key(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(tmp_path / "k"))
    state = Redactor(mode).as_dict()
    assert state["keyed"] is False and state["key_id"] is None


def test_hash_mode_in_a_fresh_environment_creates_and_reports_its_key(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACKBOX_KEY_PATH", str(tmp_path / "k"))
    state = Redactor("hash").as_dict()
    assert state["keyed"] is True and isinstance(state["key_id"], str) and len(state["key_id"]) == 8

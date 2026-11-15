"""P3B-04's second half: the three link classes, one fixture each, and the rule that an inference merges nothing.

A `LINK` is a claim about two tapes, so every test here is written the way a sceptic would read the output:

* **exact** — the proxy's tape names the run; the SDK's tape *is* that run. Confidence 1.0, and the evidence says which
  header carried it.
* **declared** — the user said so. Confidence 1.0 but the class is `declared`, so a reader can see it was not measured.
* **inferred** — a time window *and* a fingerprint match, with both numbers in the evidence and a confidence that is never
  1.0, because nobody asserted anything.

The last test is the invariant the plan puts in bold: an inferred link never merges two runs. `resolve()` returns links
and nothing else; the assertion is that the two run identities come back unchanged and distinct, so a future refactor that
"helpfully" unions them fails here rather than in a capsule a customer opens.
"""
from __future__ import annotations

from blackbox.rec import links

NOW = 1_700_000_000_000_000_000


def _call(writer: str, vantage: str, **kwargs: object) -> links.CallView:
    params: dict[str, object] = {"writer_uuid": writer, "vantage": vantage, "wall_ns": NOW, "fingerprint": "fp-a"}
    params.update(kwargs)
    return links.CallView(**params)          # type: ignore[arg-type]


def _sdk(run: str | None, calls: tuple[links.CallView, ...] = ()) -> links.RunView:
    return links.RunView.of("writer-sdk", "sdk", run, calls)


def _proxy(run: str | None, calls: tuple[links.CallView, ...] = ()) -> links.RunView:
    return links.RunView.of("writer-proxy", "proxy", run, calls)


# ── exact ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_same_run_id_is_an_exact_link_and_says_which_header_carried_it():
    resolved = links.resolve(proxy=_proxy("run-9"), others=[_sdk("run-9")])
    assert len(resolved) == 1
    link = resolved[0]
    assert link.klass == links.EXACT and link.confidence == 1.0
    assert link.evidence["carried_by"] == "x-blackbox-run"
    assert link.as_dict() == {"class": "exact", "left_writer": "writer-proxy", "right_writer": "writer-sdk",
                              "left_run": "run-9", "right_run": "run-9", "confidence": 1.0,
                              "evidence": link.evidence}


def test_an_exact_link_is_not_also_inferred_when_the_calls_also_match():
    """Two records for one relation is how a graph grows a second edge that disagrees with the first."""
    calls = (_call("writer-sdk", "sdk", fingerprint="fp-a"),)
    same = (_call("writer-proxy", "proxy", fingerprint="fp-a"),)
    resolved = links.resolve(proxy=_proxy("run-9", same), others=[_sdk("run-9", calls)])
    assert [link.klass for link in resolved] == [links.EXACT]


# ── declared ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_link_the_user_asserted_is_declared_and_not_dressed_up_as_a_measurement():
    resolved = links.resolve(proxy=_proxy("proxy-run"), others=[_sdk("sdk-run")],
                             declared_pairs=[("proxy-run", "sdk-run")])
    assert [link.klass for link in resolved] == [links.DECLARED]
    assert resolved[0].confidence == 1.0
    assert resolved[0].evidence["asserted_by"] == "user", "the record says who claimed it"


# ── inferred ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_time_and_fingerprint_match_is_inferred_with_its_evidence_and_a_confidence_below_one():
    mine = (_call("writer-proxy", "proxy", wall_ns=NOW + 400_000_000),)
    theirs = (_call("writer-sdk", "sdk", wall_ns=NOW),)
    resolved = links.resolve(proxy=_proxy("proxy-run", mine), others=[_sdk("sdk-run", theirs)])
    assert [link.klass for link in resolved] == [links.INFERRED]
    link = resolved[0]
    assert links.MIN_INFERRED_CONFIDENCE <= link.confidence < 1.0
    assert link.evidence["delta_ns"] == 400_000_000
    assert link.evidence["fingerprint"] == "fp-a"
    assert link.evidence["why_not_exact"].startswith("neither tape names")


def test_a_fingerprint_match_outside_the_window_is_not_a_link():
    mine = (_call("writer-proxy", "proxy", wall_ns=NOW + 5_000_000_000),)
    theirs = (_call("writer-sdk", "sdk", wall_ns=NOW),)
    assert links.resolve(proxy=_proxy("proxy-run", mine), others=[_sdk("sdk-run", theirs)]) == []


def test_a_time_match_without_a_fingerprint_is_not_a_link():
    """Two calls in the same second is what a busy agent looks like, not evidence that they are the same call."""
    mine = (_call("writer-proxy", "proxy", wall_ns=NOW, fingerprint="fp-a"),)
    theirs = (_call("writer-sdk", "sdk", wall_ns=NOW, fingerprint="fp-b"),)
    assert links.resolve(proxy=_proxy("proxy-run", mine), others=[_sdk("sdk-run", theirs)]) == []


def test_the_nearest_match_in_the_window_wins_and_the_window_is_configurable():
    mine = (_call("writer-proxy", "proxy", wall_ns=NOW + 300_000_000, fingerprint="fp-a"),)
    theirs = (_call("writer-sdk", "sdk", wall_ns=NOW, fingerprint="fp-a"),)
    tight = links.resolve(proxy=_proxy("p", mine), others=[_sdk("s", theirs)], window_ns=100_000_000)
    wide = links.resolve(proxy=_proxy("p", mine), others=[_sdk("s", theirs)], window_ns=1_000_000_000)
    assert tight == [] and len(wide) == 1


# ── the invariant ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_inference_never_merges_two_runs():
    mine = (_call("writer-proxy", "proxy"),)
    theirs = (_call("writer-sdk", "sdk"),)
    proxy, sdk = _proxy("proxy-run", mine), _sdk("sdk-run", theirs)
    resolved = links.resolve(proxy=proxy, others=[sdk])
    assert len(resolved) == 1
    assert resolved[0].merges_runs is False
    assert proxy.run_id == "proxy-run" and sdk.run_id == "sdk-run", "both identities are exactly what they were"
    assert resolved[0].left_run == "proxy-run" and resolved[0].right_run == "sdk-run"
    assert resolved[0].left_writer != resolved[0].right_writer


def test_a_proxy_never_links_to_itself():
    assert links.resolve(proxy=_proxy("run-9"), others=[_proxy("run-9")]) == []


def test_the_summary_always_names_all_three_classes_so_a_zero_is_visible():
    resolved = links.resolve(proxy=_proxy("run-9"), others=[_sdk("run-9")])
    counts = links.summary(resolved)
    assert counts == {"exact": 1, "declared": 0, "inferred": 0}
    assert list(counts) == list(links.CLASSES), "a capsule reader must not have to interpret a missing key"

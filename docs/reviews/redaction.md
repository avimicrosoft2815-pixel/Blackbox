# Review pack — redaction (this is not a gate file, and carries no verdict) — unsigned builder evidence

**Re-read at `1466f43`, 2026-11-21** (shipped in `044c1f8`, whose only change is this line). Every command in this pack was re-run
against that tree: 142 tests, all pass, exit 0. No row changed and no row was added; a re-run is not a review.

The claim redaction has to support is narrow and testable: **for a corpus of secrets, no sink this product renders
contains one**, and the same run with redaction deliberately switched off *does* contain them — because a green list of
sinks proves nothing if the sinks were empty.

## Rows, code and commands

| Row | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| The corpus: 60 secrets over 10 kinds, including values that collide with JSON, quotes and newlines | `tests/_secrets.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_redaction.py -q` | passes; split offsets are exercised, so a secret straddling a chunk boundary is covered |
| 11 sinks, each scanned for all 60 secrets, with the off-switch control | `src/blackbox/cap/`, `src/blackbox/export_*.py`, `src/blackbox/ui/`, `tests/test_redaction_every_sink.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_redaction_every_sink.py -q` | **3 collected, all pass**: the tape's frames, the capsule file, `otlp/spans.json`, `otlp/spans.pb`, `events/events.jsonl`, `graph.json`, `manifest.json`, the viewer page, the diagnosis report, the repair brief, and the desktop pages (`/`, `/library`, `/run/<capsule>`) |
| The control: with `BLACKBOX_REDACTION=off` the same fixture puts all 60 into the capsule | same file, third test | same command | passes — 60 redactions asserted in the on case and 60 present in the off case, so "clean" cannot mean "empty" |
| `hash` mode leaves neither the secret nor an unkeyed digest of it | `src/blackbox/redact.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_redaction.py -q -k hash` | passes |

## What a reviewer should decide

1. **Are those really all the sinks?** Walk the product's output surface: `capsules/*.bbx` members, `blackbox view`,
   `blackbox diagnose --out/--brief`, the capsule's own `events/events.jsonl` and `otlp/spans.*` (written by `blackbox diagnose` and by the capsule build), `blackbox serve`, the desktop pages and the
   desktop exports. If a reviewer finds a sink the list does not name, that is the finding.
2. **Is the corpus strong enough?** Read `tests/_secrets.py`. A weak corpus makes a green scan worthless; the cheap check
   is to add one secret of your own to the list and watch the suite fail before the fix and pass after.
3. **Does the off-switch control run the same code path?** It must differ only by the environment variable. The test's
   fixture is shared, which is what makes the pair meaningful.
4. **Redaction is not encryption.** `docs/privacy.md` and `docs/limitations.md` say so; a reviewer should not accept a
   claim that the capsule is unreadable by somebody with the file.

## What this machine cannot do

* Prove that a *new* sink added later is covered. Nothing can: the list is maintained by hand, and the test fails only for
  the sinks it knows. That is a design limit worth recording in the gate, not a defect to fix today.
* Measure redaction cost on a quiet host (there is a keyword-bearing body figure in the §15.1 artefact, for capture).

## The command

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_redaction.py tests/test_redaction_every_sink.py -q
```

If it passes on a fresh clone, the row "60-secret corpus absent from all 11 sinks" is as evidenced as a builder can make
it; the verdict stays the reviewer's.

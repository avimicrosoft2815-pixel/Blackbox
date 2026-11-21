# Review pack — malicious and malformed files (this is not a gate file, and carries no verdict) — unsigned builder evidence

**Re-read at `1466f43`, 2026-11-21** (shipped in `044c1f8`, whose only change is this line). Every command in this pack was re-run
against that tree: 60 tests, all pass, exit 0. No row changed and no row was added; a re-run is not a review.

A `.bbx` capsule is a file a user may have been sent. The reader therefore treats it as hostile input: it must refuse
early, with a documented code, inside a time bound — and the pages that render recorded content must never let that
content become markup.

## Rows, code and commands

| Row | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| The capsule attack corpus (§13.4): traversal, absolute paths, drive letters, backslashes, empty components, symlinks, encrypted and bzip2 members, a lying entry count, non-zip bytes, an unknown manifest version | `src/blackbox/cap/format.py` (checks 1–9), `tests/test_capsule_attacks.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capsule_attacks.py -q` | **25 collected, all pass**; the worst rejection was 15.718 ms against a 50 ms row |
| Every refusal is a documented code, and the CLI prints the code rather than a Python traceback | `src/blackbox/errors.py`, `docs/errors.md` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capsule_attacks.py -q -k cli` | passes |
| Frames: a torn frame, a bad CRC, a zeroed head and a corrupt meta each reach their documented code | `tests/test_fault_codes.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_fault_codes.py -q` | passes |
| 20 injected-markup payloads are inert in the viewer, the desktop pages, the Markdown report, the brief, and the capsule's own bytes | `tests/test_xss_corpus.py`, `src/blackbox/ui/viewer.py`, `src/blackbox/ui/app.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_xss_corpus.py -q` | **5 collected, all pass** |
| The independent readers (Python, Node, Go) refuse the same hostile inputs with the same codes | `tests/test_independent_reader.py`, `tests/test_conformance.py`, `tools/readers/` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_conformance.py tests/test_independent_reader.py -q` | passes here; the Go arm is compiled and run by the `go-reader` CI job, which is green on this branch's runs |
| A reader that must read an untrusted file with no network and no shell-out | `src/blackbox/platform/`, `tests/test_no_egress.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_no_egress.py -q` | 5 pass |

## What a reviewer should decide

1. **Is the time bound real?** 50 ms for a refusal is a claim about behaviour on a hostile file. The test measures the
   worst case; a reviewer should re-run it on their own host, because the bound is a wall clock.
2. **Are checks 10 and 11 of §13.4 implemented?** They are not: there is no derived-part line reader and no generated-test
   runner, so there is nothing to exercise. The attack test says so in its docstring rather than counting them as covered.
3. **Does any renderer still interpolate recorded text into markup?** The seven renderers are enumerated in the XSS test;
   a reviewer who finds an eighth should add it, because the corpus is only as good as its list.
4. **Symlinks and the capsule.** A capsule containing a symlink must be refused, not followed; the payload for that is a
   real symlink entry in a real zip. Read `test_capsule_attacks.py` for how it is built.

## What this machine cannot do

* Fuzz at scale. There is `bench/fuzz_cbor.py` for the codec; it is a smoke fuzz, not a campaign, and the pack says so
  rather than implying a fuzzing programme.
* Run the Go reader. No Go toolchain here; the CI job is the measurement.

## The command

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_capsule_attacks.py tests/test_fault_codes.py tests/test_xss_corpus.py -q
```

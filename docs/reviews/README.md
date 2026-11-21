# Review packs — what an independent reviewer needs, written by the builder

`docs/gates/README.md` says a gate file is written **by the reviewer**, after a fresh clone and a re-read of every
must-be-true row. Nothing in this directory is a gate file, a verdict or a tick. These are **packs**: for each review
area, the rows, the code that is supposed to hold them, the exact commands that show whether it does, and what this
machine saw when the builder ran them on 2026-11-20.

Two reasons a pack is more useful to a reviewer than a status table:

* **A command is re-runnable and a sentence is not.** Every row below names the file and the command; a reviewer who
  distrusts a row can settle it in a minute instead of reading a paragraph.
* **The gaps are in the same place as the evidence.** Where this machine cannot do the check — no display, no Windows, no
  quiet host, no keys — the pack says so in the row rather than in a footnote, so a reviewer cannot mistake an unrun row
  for a passed one.

| Pack | Rows it covers | State of the evidence |
|---|---|---|
| [`capture.md`](capture.md) | unmodified SDKs recorded, streaming, fail-open, the seam ladder | builder-run here |
| [`privacy.md`](privacy.md) | what is recorded, no egress, permissions, the user-facing promise | builder-run here; the Windows/macOS arms are CI cells |
| [`redaction.md`](redaction.md) | the 60-secret corpus across every sink, with the off-switch control | builder-run here |
| [`recovery.md`](recovery.md) | idempotent recovery, the kill matrix, the torn-frame codes, the capsule check | builder-run here; the 2 000-trial run is CI |
| [`malicious-files.md`](malicious-files.md) | the capsule attack corpus, hostile frames, the injected-markup corpus | builder-run here |
| [`integrations-exports.md`](integrations-exports.md) | OTLP and JSONL exports, the importer matrix, the SDK matrix, the header contract | partially: three importers measured, three blocked |

## When these were measured, and against what

The last column of every pack belongs to one commit. These packs were written and re-read at **`133b0c3`** on the branch
`arena/86af3334-blackbox`, whose CI run **`44809947917` is green on all eleven suite cells** (3 OSes × 3 Pythons, macOS
included) plus `docs` and `go-reader`, with `1 586` tests collected on the build machine (1 584 passed, 2 skipped by name).
A reviewer checking a *different* sha must re-run the commands: a number here is evidence about that commit and says
nothing about another one, even when the file has not changed.

## Re-read at `1466f43`, shipped in `044c1f8`, 2026-11-21

The builder re-ran every pack's commands against `1466f43`, because a pack pinned at `133b0c3` is evidence about
`133b0c3`. The commit these files ship in is `044c1f8`, whose only difference from `1466f43` is the table below; `ci` run
`44871213580` was green on all eleven cells at `1466f43`. This is a re-read, not a verdict: it says the commands still answer the same way here, and nothing about whether
the rows are true.

| Pack | Command it names | Collected | Result |
|---|---|---|---|
| [`capture.md`](capture.md) | `pytest tests/test_day14_kill.py tests/test_failopen.py tests/test_preflight.py tests/test_capture_failopen.py -q` | 43 | all pass, exit 0 |
| [`privacy.md`](privacy.md) | `pytest tests/test_desktop_beginner_journey.py tests/test_launcher_capture_warning.py tests/test_netguard_reads.py tests/test_no_egress.py tests/test_permissions.py tests/test_proxy_routes.py tests/test_launcher_proxy.py -q` | 48 | all pass, exit 0 |
| [`redaction.md`](redaction.md) | `pytest tests/test_redaction.py tests/test_redaction_every_sink.py -q` | 142 | all pass, exit 0 |
| [`recovery.md`](recovery.md) | `pytest tests/test_capsule.py tests/test_content_modes.py tests/test_day14_kill.py tests/test_attach_curl_kill.py tests/test_fault_codes.py tests/test_launcher_proxy.py tests/test_proxy_supervisor.py tests/test_supervisor_loop_errors.py tests/test_recovery_identity.py tests/test_verify_cli.py tests/test_retention.py tests/test_retention_dry_run.py -q` | 110 | all pass, exit 0 |
| [`malicious-files.md`](malicious-files.md) | `pytest tests/test_capsule_attacks.py tests/test_fault_codes.py tests/test_xss_corpus.py tests/test_conformance.py tests/test_independent_reader.py tests/test_no_egress.py -q` | 60 | all pass, exit 0 |
| [`integrations-exports.md`](integrations-exports.md) | `pytest tests/test_attach_sdks.py tests/proxy/test_client_matrix.py tests/test_export_jsonl.py tests/test_export_otlp.py tests/test_export_otlp_pb.py tests/test_headers.py -q` | 51 | all pass, exit 0 |

The whole suite at the same commit: **1 598 tests collected, `pytest -q` exit 0 in 444.7 s** on this 2-CPU Linux box
(1 596 pass, 2 skipped by name on POSIX). The rows these packs cannot fill — no second reader, no display, no Windows or
macOS desktop, no quiet host — are unchanged, and re-running a command here does not fill any of them.

Six things landed after the packs were first written; five of them sharpen a row, none of them ticks one:

* `docs/getting-started.md` — the eight-step beginner path, checked against the product by `tests/test_getting_started.py`.
* The Windows packaging path went green on a real runner (`44807593306`): built, installed silently, installed tree run,
  uninstalled, recordings kept. That is packaging evidence, not a gate row; the window is still unrun.
* Defect 117 — the 5 001-entry refusal went red once on Windows at 133.2 ms against a 50 ms bound and green on the next
  commit. The budget did not move; the failure message now carries a second timed refusal so the next occurrence says
  which half was slow.
* Defect 116 stayed open on purpose: a Windows path reached the CLI as `D`, the cause is unsettled, and the workflow now
  avoids the shell boundary rather than asserting a diagnosis.
* **Defect 114 is fixed and measured** (`0ca6951`): the index build ran one transaction per capsule — 1 000 commits for 500
  capsules, 922 ms incremental and 1 068 ms rebuild — and now runs one per build, 2 commits, 311.7 ms and 298.7 ms, with the
  dump hash unchanged (`bench/results/2026-11-21-index-build-cost.json`). The I8 row is the one this moves, from "reproduced
  the dump" to "reproduced the dump at a stated cost".
* **The installer was installed twice over itself** (run `44868905784`): install, run, install the same artefact again, run
  again, uninstall, recordings counted across all three. One artefact twice is the overwrite path, not a newer version
  replacing an older one, and the workflow prints that sentence itself.
* **Defect 120 records a red Windows cell this work caused nobody**: `ingest` measured 155 ms slower than the front door
  against an 80 ms budget, on a commit touching no hook code, and green on the next. The measurement is now five
  interleaved pairs with the same 80 ms bound, so the number describes our code rather than the runner's mood.
* **Defect 121 is an interpreter bug in the tree's own tests**: CPython 3.11's parser raised
  `SystemError: AST constructor recursion depth mismatch` once, mid-suite, on a file that parses fine alone. The static
  tests take one retry for that error and raise for everything else.
* **The recovery claim is published whole** (defect 101, update 3): the lock was created empty and written afterwards, so a
  peer could read nothing, judge it stale and take a live claim — which is how `44820329776` produced two recoverers for one
  run. It is now written to a temporary file and published with `os.link`, and two tests fail against the old code.
* **Defect 118 and defect 119 are diagnostics, not fixes**: on macOS a stall dump has twice held one thread, and one killed
  `curl` run reported no node statuses. Neither could be reproduced here; both failure messages now carry the numbers that
  would name the cause, and `docs/limitations.md` states the macOS stall-dump limitation where a user reads limitations.

## What none of these packs can contain

* **A second reader.** That is the whole point of a gate.
* **A window on a real desktop.** There is no display, no GTK and no WebKit in the environment that wrote these.
* **A Windows or macOS run of the desktop journey.** CI runs the suite on both; it does not open a window.
* **A quiet host.** Two §15.1 rows are labelled `on_the_bench_box: false` in their own JSON for that reason.

## How to use one

```sh
git clone <repo> && cd Blackbox && git checkout <candidate sha>
python -m venv .venv && .venv/bin/pip install -e ".[dev,capture,capture-sdks]"
PYTHONPATH=src .venv/bin/python -m pytest tests -q            # the whole thing first
```

Then open the pack for the area and run its commands. If a row fails, that is a finding, and rule 5 in
`docs/gates/README.md` applies: name the single thing that would change the verdict.

## A note on the numbers in these packs

They are today's, from this machine, with the command beside them. They are **not** a claim about a runner, a quieter
host, or a later commit. Where a number came from CI, the pack says which run.

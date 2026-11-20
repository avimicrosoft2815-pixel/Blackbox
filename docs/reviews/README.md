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

# BlackBox

**A local-first flight recorder for programs that call a model.** Run your app under it; if the app dies, the run is
already on disk, and you can read back what it was doing — offline, with no account and no server.

**pre-alpha.** One person built this with an agent in a sandbox, and the honest status is in
[`docs/STATUS.md`](docs/STATUS.md): what is green, what is pending, and what could not be tested here. Nobody has run this
on a Mac by hand; on the current commit the three macOS CI jobs **never reached a runner** — GitHub answered
"The job was not acquired by Runner of type hosted even after multiple attempts", with its own capacity notice — so
macOS is *unmeasured* here, not passing.

## Install

Python 3.11+ and nothing else at runtime. From a checkout:

```console
$ python3 -m venv .venv
$ .venv/bin/pip install -e .
$ .venv/bin/blackbox --version
blackbox 0.1.0.dev0 (format v2)
```

Offline machines need `wheel` available too: `pip install -e . --no-build-isolation` otherwise stops with
`invalid command 'bdist_wheel'`.

## The four commands that matter

Run your program. `blackbox run` starts the recorder, points the model SDKs at a loopback proxy, and recovers both tapes
into capsules when the process is gone:

```console
$ blackbox run -- python3 app.py
call failed: InternalServerError
blackbox: run r_450df8d2e53c9928 · exit 3 · proxy http://127.0.0.1:40995 (5 vars exported) · sdk auto-import (sitecustomize) · recovered r_450df8d2e53c9928 → /tmp/demo/capsules/c_c5b8af497f2a5577.bbx (recovered, root 68ebe1e85480) and 1 more tape (sdk)
```

See what is there. `ls` reads each capsule's manifest — not its entries — so it still lists a capsule whose internals are
damaged, and says so:

```console
$ blackbox ls
CAPSULE             RUN                 STATUS                   EVENTS  SIZE
c_63ffefb68a33b87c  r_450df8d2e53c9928  recovered/PROCESS_DEATH  23      23.7 KiB
c_c5b8af497f2a5577  r_450df8d2e53c9928  recovered/PROCESS_DEATH  17      20.8 KiB
```

Read the incident. `diagnose` writes a report that separates what the frames **show** from what is **likely** and what
the capsule **cannot tell you**, and quotes recorded application text as untrusted input:

```console
$ blackbox diagnose capsules/ --out incident.md
$ blackbox view capsules/ --out incident.html     # one offline HTML page, no server, no network
```

Check the record. `verify` recomputes every check from the capsule's own bytes and exits 9 when one fails — a single
flipped byte in a segment is reported **by entry name**, not just as "corrupt":

```console
$ blackbox verify capsules/c_63ffefb68a33b87c.bbx
capsules/c_63ffefb68a33b87c.bbx
  ok   integrity
  ok   chain_root
  ok   derived_parts
  ok   capture_seam
  ---- capture_vantage  a capsule carries no meta page: the vantage is declared and cannot be recomputed here, so it is printed as unverified rather than counted as a pass
  ok   links_summary
blackbox verify: 1 capsule(s) verified; 1 check(s) declared, not checkable from a capsule
```

### Approving a fix

`diagnose` produces evidence and a brief; it never edits your code. A fix arrives as a proposal (from you or an agent),
and `repair` is the gate in front of it: `preview` prints the diff and a token bound to the proposal, the repository and
the current hash of every file it touches; `apply` refuses without that token, and refuses if any of those files changed
after the preview. Nothing is ever run except the tests you name at that moment.

```console
$ blackbox repair preview fix.json --repo .
$ blackbox repair apply fix.json --repo . --approve 43acebb6b1dd0ac17d4174c1de4c7ef1
wrote   app.py
originals saved in /tmp/demo/.blackbox-repair/backup-20261117T164351-43acebb6
nothing was run: this change is unverified until you run `blackbox repair verify --test …`
$ blackbox repair verify fix.json --repo . --test "python3 -m pytest -q"
PASS  exit   0    0.02s  python3 -m pytest -q
```

Data lives where you ran the app (`./tape/`, `./capsules/`); `--data-dir` puts it elsewhere. `blackbox ls`, `verify
--data` and `diagnose` read from the current directory when `--data`/`$BLACKBOX_DATA_DIR`/`$BLACKBOX_HOME` says nothing.

## What this does not do — read this before trusting it

- **Node-to-node links are not recorded.** A run's capsule has run/tape edges and per-call edges, but **0 node-level
  edges**: the proxy's vantage has no node identity, so no edge joins two recorded nodes (P3B-03, defect 55,
  [`docs/node-edges.md`](docs/node-edges.md)). What a call belongs to is `exact`, `partial` or `ambiguous`, and it is
  labelled as such rather than guessed.
- **The SDK seam covers `openai`, `anthropic` and plain HTTP clients that honour the exported base URL** — a program with
  no instrumentation gets no node, and the capsule says so ("no node" is the honest answer, not a blank).
- **Importers:** only Phoenix has been tried, and it accepts `otlp/spans.pb` (200, read back intact) while rejecting
  `otlp/spans.json` (415). Langfuse, the OpenTelemetry Collector's OTLP/JSON receiver and Phoenix's gRPC port 4317 are
  **untested**.
- **Platforms:** Linux is measured here. All three Windows cells and all three Linux cells pass in CI on this branch.
  **Every macOS cell is unmeasured**: the jobs were never acquired by a runner (GitHub's macOS arm64 capacity limit), and
  nobody has run this on a Mac by hand. The last macOS result this repository holds is from `66ece0f`, where 3.11, 3.12
  and 3.13 passed.
- **Durability is per failure class, not universal.** Process death is tested (500 randomized `SIGKILL` trials, 0
  violations); power loss is not claimed. See [`docs/guarantees.md`](docs/guarantees.md).
- **The watchdog is best-effort.** If the proxy dies, the listener comes back in p50 0.76 ms and the measured worst
  outage was 51.9 ms on Linux — with no universal 100 ms guarantee (defect 58).
- **A wide batch is delayed, and a very wide one is rejected — on purpose, and visibly.** The recorder holds at most
  512 connections and passes through at most 200 model requests at a time on a host with the usual 1024-descriptor
  budget, and **both numbers are planned from the descriptor budget the machine actually has** (a macOS shell that starts
  at 256 gets 96 connections and 64 in flight; a container with less gets less). Past the in-flight ceiling a request
  **waits** for a slot — nothing is dropped, and the wait is counted in the run's output. Past the connection ceiling, or
  after 60 s of waiting, the request is **rejected** with `503` and `x-blackbox-error: connection-limit` /
  `queue-timeout` plus a body line saying what to do; those requests never reach a provider. A run that hit either rule
  says so in one sentence after its summary, naming the delay and the rejection separately
  ([`docs/RELEASE-BLOCKERS.md`](docs/RELEASE-BLOCKERS.md) B3, defects 62, 69, 71, 72).
- **A client that disconnects while queued is noticed late.** The slot is returned and the departure is counted, but the
  request it abandoned may already have reached the provider, because the recorder cannot see a socket close while it is
  parked waiting for a slot. Mid-stream disconnects are recorded as such (`client_gone`).

## Where to read next

- [`docs/STATUS.md`](docs/STATUS.md) — every ticket, its test path, and its result.
- [`docs/guarantees.md`](docs/guarantees.md) — what survives which failure, and what is not claimed.
- [`docs/format.md`](docs/format.md) — the tape and capsule formats, byte by byte.
- [`docs/onboarding.md`](docs/onboarding.md) — three tasks for someone who has never seen this repository.
- [`docs/progress.md`](docs/progress.md) — the ledger of record.

## Licence

`pyproject.toml` declares Apache-2.0, but the repository has **no `LICENSE` file** — the metadata and the tree disagree,
and the owner has to settle it. Until then, ask before redistributing.

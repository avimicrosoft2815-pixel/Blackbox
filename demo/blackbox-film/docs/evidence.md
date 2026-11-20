# The incident the film shows

Everything in scenes 3–5 comes from one run of this repository's CLI against a synthetic application. This page is the
index to that run: what was executed, what came out, and what the record cannot answer. The files themselves are in
`evidence/`.

## Reproduce it

```bash
cd demo/blackbox-film
BLACKBOX=../../.venv/bin/blackbox PYTHON=../../.venv/bin/python bash incident/run_demo.sh /tmp/bbdemo
python3 tools/scrub_evidence.py /tmp/bbdemo evidence/
```

`incident/app.py` asks a model for a message, then makes a tool call it never reaches. `incident/mock_provider.py` answers
with chunked SSE framing, sends three deltas, and closes the socket without a terminal event — a truncated stream, which
is the failure the film is about. `run_demo.sh` starts the mock on loopback, runs the app through `blackbox run`, recovers
the capsule, verifies it, and asks for a diagnosis and a brief.

## What was recorded

| | |
|---|---|
| Run | `r_83cb737e8089200e` |
| Capsules | `c_75c1d97257dac250` (launcher, 5 events), `c_b3820c3ee321dd9d` (sdk, 17 events) |
| Run status | `recovered` / `PROCESS_DEATH` — the app exited before the run closed itself |
| Frames | 17, of which 8 are `CHAIN` bookkeeping and 8 are events plus `SEG_END` |
| App exit code | 3 |
| Streamed | 2 × 92 B to the client, 776 B in total reached the client and the proxy |
| Verify | 2 capsules verified; 1 check declared, not checkable from a capsule |

The event sequence the timeline scene re-draws, exactly as `evidence/report.md` prints it:

```
1  RUN_START     run_id=r_83cb737e8089200e
3  NODE_OPEN     node 16384  POST /v1/chat/completions  kind=proxy_call
5  MODEL_REQUEST provider=openai  stream=True
7  MODEL_CHUNK   92 B
9  MODEL_CHUNK   92 B
11 MODEL_ERROR   partial=True  status=200  INTERRUPTED_UPSTREAM
13 NODE_CLOSE    outcome=error  status=200  INTERRUPTED_UPSTREAM
15 RUN_END       exit_code=3
17 SEG_END
```

## What the diagnosis says

Two findings, both `observed`, both confidence `high`:

1. **the step 'POST /v1/chat/completions' ended with an error** (`node-outcome-error-16384`) — evidence `seq 13
   NODE_CLOSE` and `seq 3 NODE_OPEN`. The tool names two other explanations that fit the same frames: the application
   raised and the node's own close recorded it, or a recorder error closed the node early.
2. **the provider returned an error at node 16384** (`provider-error-16384`) — evidence `seq 11 MODEL_ERROR` and `seq 5
   MODEL_REQUEST`. What would settle it: a retry that succeeded would appear as a second `MODEL_REQUEST`.

And what it explicitly cannot tell you, in the report's own words: *content is stored as keyed or unkeyed hashes: prompt
and response text cannot be recovered from this capsule, and no claim here is about their wording.*

The film quotes that limit rather than smoothing it over. In scene 4 the review track ends in a dashed `UNKNOWN`; in
scene 5 the third telemetry card is dashed, dim, and its meter stops short of full.

## The repair beat in scene 6

The code-diff scene shows a change that was proposed, approved and verified during production, on the synthetic repo in
`incident/repo/` (pre-fix, one failing test on purpose):

- `blackbox repair preview incident/proposal.json --repo …` printed the diff and issued an approval token.
- `blackbox repair apply --approve <token>` wrote the file, left a backup, and said: *nothing was run: this change is
  unverified until you run `blackbox repair verify --test …`*.
- `blackbox repair verify --test "… -m pytest -q"` printed `PASS`, exit 0, 0.36 s, `2 passed in 0.14 s`, and noted that
  the proposal's own suggested tests were not executed — only the check that was named.

The frame carries both halves: the PASS chip and the caveat line under it. The token in the frame is truncated to twelve
characters; the full token is not reproduced in the film or in these documents, because it authorised one specific write
to one specific file and has no further use.

The proposal's JSON uses the key `tests`, not `suggested_tests`: the latter is accepted silently and shown as an empty
list. That was a real defect found while making this film, and it is recorded here rather than quietly fixed in the
transcript.

## What this run is not

It is not a customer's incident, not a real provider, and not a network call to anything but loopback. The provider is a
mock written for this demo; the application is a synthetic order desk. The numbers are real outputs of real tools on that
synthetic input, and the film says *illustrative scenario* wherever a viewer might otherwise take them for a production
case.

## Scrubbing

`tools/scrub_evidence.py` rewrites a raw work directory into `evidence/`. It replaces this machine's absolute paths with
`<repo>` and `<tmp>`, removes the process id, splits the run summary into `run.json` (machine-readable) and `run.out` (the
app's own line), and then re-scans its own output: if any home path, temp path or pid survives, it exits non-zero instead
of leaving a leak for a later commit. Run ids, capsule ids, sequence numbers, byte counts, timings and the tools' own
wording are kept — deleting them would make the film's claims unverifiable.

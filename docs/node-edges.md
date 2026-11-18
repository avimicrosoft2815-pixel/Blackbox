# Node-to-node edges: what was built, what it measured, what is still a proposal

**Status: shape (a) of this note is implemented and measured (2026-11-18).** The proxy now records a span of its own for
every request it forwards, so a call that crosses the seam and the proxy has a recorded node on **both** tapes and
`graph.json` carries `level: "node"` edges whose two ends are spans their own writers opened. Two parts of the note stay
proposals and are marked as such: **shape (b)** (a join between an app span and a hook session's tool span) and the
**launcher-owned process span**. Nothing here invents a node: every end of every edge in the measurement below appears in
some tape's own node table with an `NODE_OPEN`/`NODE_CLOSE` pair.

The plan-level facts that gate what a reader may conclude are unchanged: D-052 rules 1–2 (both ends recorded, no node in
an edge that is not on a tape) and D-053 (the call id is the join). What changed is that the proxy now has a span to put
on its side.

## What the edges say now

| | before 2026-11-18 | now |
|---|---|---|
| `level` | `call` (or `run`/`tape`) | `node` when both sides recorded a span, otherwise `call` |
| `nodes.from` / `nodes.to` | one slot and `null` | the app's node slot and the proxy span's slot |
| `node_join` | `partial` (one end names a node) | `exact` (both ends), `partial` (one end), `ambiguous` (`candidates`), `unknown` (neither) |
| proxy's node table | empty (`MODEL_*` frames at slot 0, the run itself) | one `proxy_call` node per forwarded request, `owner: "proxy"` |
| `edge_summary.node_join_reason` | "the proxy vantage records calls, not the app's nodes" | says which of the three cases applies, in the same words a reader sees |

The edge is still symmetric and still says **which writer owns which end**, and `evidence` is unchanged:
`{"rule": "same call id", "carried_by": "x-blackbox-call"}`. A shared run id, a shared trace id, a matching fingerprint or
two calls that happen to be near each other in time produce **no** node edge — the resolver only joins on the id the seam
minted and the proxy read, and the negative-control case below is a run of exactly that shape.

## How the proxy's span is recorded

* **Two frames per forwarded request** (`src/blackbox/cap_sdk/sink.py::TapeSink.span_open/span_close`): `NODE_OPEN` with
  `name` (`POST /v1/chat/completions`), `kind: "proxy_call"`, `owner: "proxy"`, `route`, `call_id` and `linked_run`;
  `NODE_CLOSE` with `outcome` (`ok` / `error` / `not-recorded`), `status` and a duration measured by **this** writer.
* **A slot band per writer** (`src/blackbox/constants.py`): hooks number from 1 to `NODE_SLOT_HOOK_MAX` (0x3FFF), the
  proxy from `NODE_SLOT_PROXY_BASE` (0x4000) to `NODE_SLOT_PROXY_MAX` (0x7FFF). Two writers on one tape cannot collide, so
  a node slot is an identity a scanner can attribute rather than a number two tables both used. A proxy that exhausts its
  band stops opening spans and counts them (`ProxyStats.spans_capped`); the call is still recorded, with no span, and the
  edge falls back to `level: "call"` — the honest answer, not a node with no frames.
* **The same slot on the call's frames**: `MODEL_REQUEST`, `MODEL_CHUNK` and `MODEL_RESPONSE` carry the span's slot, so
  `correlate.view_of` reads a `CallView` whose `node_slot` is the proxy's own span and `links.resolve_calls` can join it to
  the app's node by `call_id`.
* **No span when nothing will be written.** The proxy asks its receiver for the span and treats a `False` answer (no sink
  behind it, or a write that raised) as "no span": a slot the graph would believe in but that no frame carries is exactly
  the invented node D-052 forbids.

## What it measured (2026-11-18, this checkout)

`python bench/accept_p3b03.py --out bench/results/2026-11-18-b4-node-edges.json` — four real clients, each launched by
`blackbox run` against a real loopback origin: `urllib` (no seam), Node v22.22.3 (no seam), `openai` 3.51.0 as shipped,
and `openai` 3.51.0 inside `api.run("answer")`.

| client | capsules | roots | one run id | nodes in graph | `node_join` | node-level edges | proxy spans |
|---|---|---|---|---|---|---|---|
| `python` (no seam) | 2 | ok | yes | 1 | `unknown` | **0** | 1 |
| `node` (no seam) | 1 | ok | yes | 1 | `unknown` | **0** | 1 |
| `sdk` (`openai`, no `api.run`) | 2 | ok | yes | 1 | `partial` | **0** | 1 |
| `sdk-nodes` (`openai` inside `api.run`) | 2 | ok | yes | 2 | `exact` | **2** | 1 |

The verdict object the bench writes says the same in machine-readable form: `node_level_edges_recorded` is
`{"node": 0, "python": 0, "sdk": 0, "sdk-nodes": 2}`, `node_join_per_client` is
`{"node": ["unknown"], "python": ["unknown"], "sdk": ["partial"], "sdk-nodes": ["exact"]}`, and
`uninstrumented_clients_stay_below_node_level` is `true`. The two node-level edges of the instrumented case are the same
join seen from each tape's own graph (the app's tape and the proxy's tape each list the edge from their side); the pairs
themselves are in `node_level_pairs` in the artefact, with both slots and the call id.

**The four cases `docs/node-edges.md` asked to measure, and where each is held:**

1. **Concurrency** — two calls in flight at once get two spans, each frame in its own, and each joined to the right span:
   `tests/test_proxy_spans.py::test_two_concurrent_calls_get_two_spans_and_each_frame_is_in_its_own` (a real origin that
   delays, so the two requests really overlap) and, on a real launch,
   `tests/test_launcher_proxy.py::test_two_concurrent_calls_get_two_ids_and_two_graph_edges_each_naming_its_own_node`.
2. **Ambiguity is a refusal** — one id recorded twice on a side writes `level: "call"`, `node_join: "ambiguous"` with
   `candidates: 2`, and names no node: `tests/test_proxy_spans.py::test_one_id_recorded_twice_on_a_side_is_never_a_node_edge`,
   plus the call-level version that already existed in `tests/test_call_links.py`.
3. **Missing instrumentation** — a child with no seam leaves a proxy span and no app node, so the edge stays `call` with
   `node_join: "partial"`; a run whose call carries no id leaves nothing joinable at all (`unknown`). Both are in
   `tests/test_proxy_spans.py` and on a real uninstrumented child in
   `tests/test_launcher_proxy.py::test_an_uninstrumented_child_leaves_no_node_and_no_call_edge`.
4. **The negative control** — no node edge from a fingerprint or a time window: the resolver's only join key is the call
   id, and the bench's `python`/`node` rows (uninstrumented clients whose frames nonetheless carry fingerprints and
   timestamps) produce **0** node-level edges while the proxy's span for them exists.
5. **The capsule verifying its own graph** — every capsule in the bench passes its own integrity root (`roots True`), and
   `graph.json` is a declared derived part recomputed from the capsule's raw frames (`blackbox capsule verify`).

## What is not measured, and stays a proposal

* **Node clients cannot produce a node-level edge yet.** `api.run` is Python-only; the Node client here has no node API,
  so its run's edges are `unknown` at node level. That is a missing feature, not a passing measurement.
* **Shape (b), the hook join, is not built.** A hook session's tool span and the SDK's call span still have no shared id
  on the wire, and the privacy question the note raised (a hook event comes from a third-party tool) is unanswered, so
  the resolver never joins them. A hook's node remains a node on its own tape with no cross-tape edge.
* **The launcher process span is not built.** If it is, it must arrive with a kind and an owner that say
  `process`/`launcher`, because it is a launcher observation and not an app node — the note's rule, unchanged.
* **`x-blackbox-node` on the wire is not built.** The proxy learns the app's node only from the tape side (the seam's own
  record of its slot), not from a header, so the proxy's graph does not name the app's node — it names its own span and
  the call id. Naming the app's node from the proxy's side is still an open decision, and nothing in the graph needs it.
* **macOS.** Every measurement above is Linux (this checkout). The same rows on macOS are unmeasured, not passed.
* **Two limits found while measuring, both now in `docs/defects.md`**: sink frames are JSON while `recovery._payload`
  decoded only CBOR (defect 75 — the first run's proxy span arrived with `kind: null, owner: null`), and
  `SinkRecorder._forward`'s parameter name collided with a span's own `name` field (defect 76 — the span was swallowed by
  fail-open and `spans` stayed 0). Both were found because the numbers were read back from the tape rather than from a
  counter, which is the reason this section quotes frame counts and slot pairs instead of "spans opened".

## What a reader may conclude from a `level: "node"` edge

The app's span that contained call C and the proxy's span for call C are two records of **one call**, joined by the id
both writers wrote. It is not a claim that the two spans are the same span, not a claim about the provider's behaviour,
and not a claim that the proxy observed the app's internals. An edge that could not be justified is written at `call`
level with a reason, and a join that had two candidates is written as `ambiguous` with the count rather than guessed.

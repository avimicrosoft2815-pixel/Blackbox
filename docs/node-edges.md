# Node-to-node edges: what would have to be true

**Status: proposal. Nothing in this document is implemented, and no edge in any graph is relabelled by it.** D-052 and
D-053 decided what the graph may say today; this note is the owner-requested answer to "what would two genuinely
recorded nodes and the evidence joining them look like, *before* we build or rename anything". P3B-03 stays unticked and
defect 55 stays open until something in the last section exists and is measured on real clients.

## What the graph says today, and what it does not

A call that crosses the seam and the proxy is recorded twice, once by each writer. D-053 gives both records the same
`x-blackbox-call` value, so `graph.json` can carry an edge with:

| today | value |
|---|---|
| `level` | `call` |
| `class` | `exact` — same call id, nothing else |
| `evidence` | `{"rule": "same call id", "carried_by": "x-blackbox-call"}` |
| `nodes.from` | the slot the call's frames sat inside **on that writer's tape**, or `null` |
| `nodes.to` | `null` in every real case, because the proxy records no node |
| `node_join` | `partial` (exactly one end names a node), `ambiguous` (an id appeared twice on a side), `unknown` (neither) |

So the current relation is precise and narrow: **a proxy record and a node on the app's tape are associated by a per-call
id, and the association is stated on the app's side of the edge.** It is *not* a claim that the proxy observed a node,
and it is *not* a claim that two recorded spans are the same span. `node_join: "partial"` is the marker that says so, and
the acceptance report counts it (`node_level_edges_recorded: 0`, `edges_naming_a_node: 2`).

An uninstrumented child (`blackbox run -- node app.js` with no SDK seam) records no node at all, so its run has nothing
for the proxy's call to attach to. That is the honest reading and it stays: no node is created to give an edge an end.
Two further facts about that case, both measured in
`tests/test_launcher_proxy.py::test_an_uninstrumented_child_leaves_no_node_and_no_call_edge`: the proxy's record of such
a request carries `call_id: null` **and** `linked_run: null` (both values come from headers only the seam writes), and
the run still needs no link for it — the proxy's frames are emitted onto the launcher's own tape, whose `RUN_START`
declares the run. So an uninstrumented child is not a missing link; it is a run whose only vantage is the proxy's, with
the proxy's records inside it.

## What "two genuinely recorded nodes" requires

Four conditions, all necessary:

1. **Both ends are recorded spans.** Each node exists because its own writer emitted `NODE_OPEN` and `NODE_CLOSE` for it
   and lists it in its own `graph.json` with a slot. A node that exists only in an edge is an invented node and is out of
   scope by D-052 rule 2.
2. **Both ends have an identity that survives crossing tapes.** A `node_slot` does not: every tape numbers from 1, so
   slot 3 on the app's tape and slot 3 on the proxy's are unrelated. Identity has to be `(writer_uuid, node_slot)` or a
   value the owning writer minted and either wrote in the node's payload or sent on the wire.
3. **The join is a per-call id both writers wrote**, not a time window and not a fingerprint. The call edge is the
   evidence; a node edge is the conclusion that the two *recorded spans* the call sat inside are being described
   together. The direction is symmetric; the edge records which writer owns which end.
4. **Ambiguity is a refusal, not a tie-break.** If one side recorded the call inside two different nodes, or an id
   appears twice on a side, no node edge is written: the edge keeps `level: "call"` with `ambiguous` and the candidate
   count. This is the same rule D-052 wrote for the run level, applied one level down.

## The two shapes that would qualify

**(a) A proxy-observed span per upstream call.** The proxy is a writer; it can open and close a span of its own for each
request it forwards (`kind: "proxy_call"`, `writer: proxy`), with its own slot, name and duration. That is a real
observation, not an app node, and it is attributable to the proxy. A node-to-node edge would then say: *the app's span
that contained call C and the proxy's span for call C are two records of one call* — the call id is the evidence, and
both nodes exist because their writers said so. The graph would need: the proxy's node table (today it records none),
`x-blackbox-node` on the wire only if the app's node identity is to be named from the proxy's side, and an edge schema
with `level: "node"` and `nodes: {from: {writer, slot, name}, to: {writer, slot, name}}`.

**(b) Two instrumented vantages of one call.** An app span (seam A) and a hook session's tool span (vantage 3) can both
be recorded nodes, but today nothing on the wire joins them: the hook path carries a session id, not the SDK's call id.
Joinable only if the SDK's call id is written into the hook event, which is a separate change with its own privacy
question (a hook event is produced by a third-party tool, not by our seam).

Shape (a) is the one worth costing first. It is small in code (two frames per proxied call) and large in honesty: it
lets the proxy describe its own span and never lets it describe the app's.

## What a launcher-owned process span would be

A node the launcher opens for the child process would be a **launcher observation**, not an app node: its writer would be
the launcher, its `kind` would say `process`, and it would exist for every run — including one whose child records
nothing. It would give a run a span to point at, and it would *not* by itself prove that a particular proxy call happened
inside a particular app node; a call edge would still carry whatever node evidence it had. If it is built, every graph
and every reader must be able to tell the two apart, which means the node entry needs an owner and a kind, not a flag on
the edge.

## What would have to be decided first

1. Whether the proxy opens a span per request at all, and what it costs under load (two frames per call, and a node
   table per tape).
2. Node identity on the wire: `x-blackbox-node` (`<run>/<node>` per the plan) or a minted node uid — and what the proxy
   may write into *its* graph about a node that belongs to another tape.
3. The node-edge schema, including what a reader may conclude from it and how an ambiguous join is written.
4. Whether a launcher process span exists, and how readers distinguish it from an app node.

## What must be measured before any of it is claimed

Concurrent calls (two calls in flight, each in its own span, both joined to the right proxy span); ambiguity (an id
recorded twice on a side — no node edge, `ambiguous` with the candidate count); missing instrumentation (a child with no
seam, a call with no id, a proxy record with no node — no node edge); and the negative control, that none of these cases
produces a `level: "node"` edge from a fingerprint or a time window. On real Python *and* Node clients, with the capsule
verifying the graph it ships.

The tests that already cover the call-level versions of those three cases are
`tests/test_call_links.py::test_two_concurrent_calls_stay_two_links_each_with_its_own_node`,
`::test_one_id_recorded_twice_on_a_side_is_ambiguous_and_names_no_node`,
`::test_an_id_on_one_side_only_is_not_a_link` and, on real runs,
`tests/test_launcher_proxy.py::test_two_concurrent_calls_get_two_ids_and_two_graph_edges_each_naming_its_own_node` and
`::test_an_uninstrumented_child_leaves_no_node_and_no_call_edge`.

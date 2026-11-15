# REVIEW — BBX-ARCH-1.1 (`BLACKBOX_FINAL_ARCHITECTURE_v1.1.md`)

**Reviewer:** architecture/implementation-planning agent
**Date:** 2026-11-14
**Reviewed artifact:** `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` — 2,738 lines · 40,273 words · 262 KB
**Supersedes:** `docs/archive/BBX-ARCH-1.0.md` (BBX-ARCH-1.0) — now marked SUPERSEDED in-file
**Status of this review:** findings only. **No patches applied**, **nothing committed**. Say "apply" and I cut a v1.2 with F1–F13 folded in.

---

## 1. Verdict

**Adopt it.** 1.1 is a real improvement over 1.0, and five of its changes are ones I would have had to make anyway:

| Change | Why it stands |
|---|---|
| **CH-02 rolling segments** (kills ring wrap + spill + in-place eviction) | Removes the largest single hazard class in 1.0: overwriting committed bytes in a live mapping. One retention mechanism instead of three, one fewer cold path to kill-test, and `retention.keep_hours = 24` becomes a config line instead of a daemon. |
| **CH-10 preallocated, never-sparse segments + SIGBUS argument** | This is the defect I missed in 1.0: a page fault into a hole under `ENOSPC` raises `SIGBUS` **in the user's process**, which is a fail-open violation. Correctly diagnosed and turned into kill stage S17. |
| **CH-03 four seams chosen by preflight** | The single-seam design was the top fragility risk (R1). Declaring `capture_seam` in the manifest is the right fix — the artifact tells you how much to trust it. |
| **CH-05 trigger engine with `stop_delay` / `cooldown` / `max_per_24h` / fingerprint** | 1.0 had "rules = [...]" and no semantics. Copying Perfetto/dotnet-monitor shape instead of inventing it is the correct instinct. |
| **CH-04 hash chain + CH-15 egress rule** | Both close holes I left: "tamper-evident" was asserted without a mechanism, and "no outbound socket" was falsified by the very proxy 1.1 introduces. |

**But it is not sign-off ready.** There are **4 blockers** (F1–F4), all of the same species: a new capability (proxy, retention, compaction, multi-capsule triggers) leaks past a guarantee that the rest of the document promises. That is normal for a +188 h design revision, and each fix is small.

The one strategic reservation is in §5 below: 1.1 grows v1.0 from 728 h to 916 h (+26 %) and moves **attach** ahead of **capsule**. My recommendation is a one-line phase swap, not a scope cut.

---

## 2. Mechanical QA (what I actually ran)

| Check | Method | Result |
|---|---|---|
| Table integrity | unescaped-pipe column count per table block | **0 defects** across 2,738 lines |
| Internal references | extract every `§N[.M]` → match against headings | **0 dangling** (2 apparent misses are `R1-§7.6`/`R1-§7.7`, i.e. references to the *critique*, not this doc) |
| Code fences | count ``` vs ```mermaid | balanced: 42 fences, 3 mermaid diagrams |
| Ledger arithmetic | sum each phase's `Est` column vs its header | **P10 mismatches: rows = 82 h, header = 72 h** (F10) |
| Stale 1.0 wording | grep `ring_bytes`, `spill`, `WRAP_PAD`, `16 MiB ring` | 6 residuals, all low severity (F11) |
| Consistency of new claims vs old guarantees | manual, seam-by-seam | 4 leaks found (F1–F5) |

I did **not** re-verify 1.1's newly added external citations (`mmap(2)` SIGBUS, `opentelemetry-lambda #2537`, Perfetto `TriggerConfig`, dotnet-monitor rules, `llm-cassette`). They are plausible and correctly labelled, but they are **unverified in this pass** and must be re-run with dates in P11-11 before any of them reach the README.

---

## 3. Findings

Severity: **B** = blocks sign-off (breaks a promise the doc makes), **M** = must fix before the phase ships, **L** = cosmetic/arithmetic.

### F1 — B · Redaction cannot run on compressed bodies (§11.8 × §12.2)

§11.8: *"`Accept-Encoding` is left alone (we store what the client would have received; decoders handle gzip/br when the body is needed)"*.
§12.2: redaction runs **before** anything is hashed, stored, exported or displayed.

Both cannot hold. A gzip/br/`zstd` body is opaque to the pattern pass, so the only way to honour §12.2 is to **decompress before redaction** — and then either store the decoded form or re-encode after redaction. As written, a compressed response containing an API key would be persisted verbatim and only decoded later, after the privacy boundary was crossed, and the 60-secret corpus test would still pass because the corpus is uncompressed. That is the exact failure mode §12 is supposed to prevent.

**Patch:** in §11.8 replace the `Accept-Encoding` sentence with:

> `Accept-Encoding` is passed through unchanged so the client receives the encoding it asked for. **The tee decompresses before redaction and never persists an un-redacted encoded body:** the proxy records `content_encoding_original`, decodes (`gzip`, `deflate`, `br` via stdlib `gzip`/`zlib`/optional `brotli`), runs the §12.2 pipeline on the decoded bytes, and stores `decoded: true` in the event and blob headers. If a codec is unavailable, the body is **not stored**: the event keeps length + keyed hash of the *encoded* bytes and `content_mode: metadata_only` with `BBX-E152 CODEC_UNAVAILABLE`. Seams A/B/C follow the same rule. Test: **S19** — the 60-secret corpus served gzip/br/zstd ⇒ zero occurrences in tape, blob, capsule, index.

Also add to §12.2 the failing-stage rule: a decode failure is treated like a redaction-hook failure (fail-closed, content dropped, event retained).

### F2 — B · Retention can delete the evidence of a run that is still open, pinned, or being diffed (§8.6)

§8.6: *"A segment that contains frames of a run that is still **open** is deleted only when budgets force it"*. There is no pin concept anywhere in the document, and §9.3 has no pin table. Consequences:

- A 6-hour agent run can lose its **middle** while still running; the capsule then says `completeness: gapped` for a run that never misbehaved.
- `baseline` rows (§9.3 `tag = 'last_good'`) point at a run whose segments may already be gone — every diff then refuses, and the user experiences "diff is broken".
- A capsule build or a `diff` in flight can `ENOENT` mid-read.

**Patch:** add to §8.6, before the retention bullets:

> **Pin set (never deleted).** `pins.json` (atomic replace, ≤ 4 KiB) lists `run_id`s that must remain readable: the **currently open** run, every run referenced by `baseline`, every run with `state != SEALED` that is younger than `keep_hours`, and any run named by `blackbox pin <run>`. A segment intersecting a pinned run is **not** a retention candidate; if the budget cannot be met without it, the keeper emits `BBX-E150 RETENTION_BLOCKED_BY_PIN{run_id, bytes_needed}` and reduces content policy (`full → hash`) for that writer instead of deleting. `blackbox pin/unpin/ls --pinned` expose this; `run.json` records when a pin saved a run.

And in §9.3: add `CREATE TABLE pin (run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, set_ns INTEGER NOT NULL, expires_ns INTEGER) STRICT;` — the index mirrors `pins.json`, `pins.json` wins.

### F3 — B · The `GAP` tombstone does not carry the chain link, so retention is indistinguishable from tampering (§8.6 × §8.12)

§8.12: *"a deleted segment is detectable as a chain break even if its footer is gone"* — and then, two paragraphs later: *"A retention `GAP` is **not** a break: the tombstone carries the chain value at the deleted range's end"*.
§8.6 defines the `GAP` payload as `{from_seq, to_seq, reason, segment}`. **There is no chain field in the tombstone.** `blackbox verify` would therefore report every retention deletion as `BBX-E223 chain break` — the check fires on the system's own normal operation, which is the fastest way to make a verification feature useless.

**Patch:** in §8.6, redefine the tombstone:

> `GAP` payload v2: `{from_seq, to_seq, reason: RETENTION|EVICTED|CORRUPT, segment_from, segment_to, chain_before: 32 B, body_hash_last: 32 B, chain_after: 32 B}`. `chain_after` is the chain value that the **last deleted segment's** `.seal` would have produced (`blake2b(chain_before ‖ body_hash_last)`), so verification resumes exactly after the declared hole. A `GAP` whose `chain_after` does not equal the chain recomputed from the surviving next segment's `prev_chain` is `BBX-E223` **with reason `UNDECLARED`** — i.e. real tampering.

And in §8.12 verification: state that the walk treats a `GAP` as a *declared hole* only when its `chain_after` matches; otherwise it is a break.

### F4 — B · `.seal` is declared immutable, but compaction invalidates two of its fields (§8.6 × §8.7)

§8.6 compaction: *"a sealed segment may be rewritten with `codec = zlib` … same input hash ⇒ same output"*.
§8.7 `.seal` stores `body_len` (bytes of valid frames) and `body_crc32c`, and the rules say a `.seal` is *"written once at close"* and *"immutable"*, with *"any later mismatch between the body and the footer … `BBX-E224`"*.

After compaction the on-disk body differs (compressed), so either the crc no longer matches (→ every compacted segment quarantined) or compaction rewrites the footer (→ "written once" is false). `body_hash` (over uncompressed bytes) is the only field that survives, which is a hint at the right answer.

**Patch:** in §8.7, change two field semantics:

> `body_len` = **uncompressed** bytes of valid frames; `body_crc32c` = CRC32C over the **uncompressed** body; `body_hash` = blake2b-256 of the **uncompressed** body. Verification decompresses when `codec ≠ 0` (bounded by `body_len ≤ segment_bytes`, so a decompression bomb is impossible by construction). The `.seal` therefore never changes: compaction rewrites `<n>.seg` only (`tmp → replace`), and `codec` lives in the **segment header**, which compaction is allowed to rewrite as part of the replace.

Add `S20`: compact a segment, kill between `tmp` write and `replace`, assert old segment + seal still verify.

### F5 — M · Seam C over-claims `capture_coverage = FULL` (§11.2 × §6.2)

§6.2 defines `FULL` as *"request start, all response chunks, terminal event, usage, and **raw payloads** per content policy"*. Seam C captures the SDK's **parsed** objects: no headers, no raw bytes, no SSE frames, retries invisible.

**Patch:** add a fifth level to §6.2, `SEMANTIC` — *"normalized model call, parsed stream events, usage, tool calls/refusals; no raw HTTP"* — and set seam C to `SEMANTIC` in §11.2, replacing the sentence *"`capture_coverage = FULL` is still truthful because nothing semantic is lost"*. Replay and diff are unaffected; only the artifact's honesty changes.

### F6 — M · Seam C's stream wrapping must preserve the SDK object contract (§11.2)

Not addressed anywhere. Wrapping `Completions.create` means the object handed back to the user is ours, and frameworks do: `isinstance(stream, Stream)`, `with stream:`, `stream.close()`, `stream.response`, `with_raw_response`, `async for` on `AsyncStream`, and `StopIteration`-sensitive `next()`/`__anext__` semantics. A naive generator wrapper breaks all of them, and the breakage appears in *user* code (fail-open violation).

**Patch:** add to §11.2:

> Seam C never substitutes a generator for an SDK stream object. It returns the **original** object with its iterator tee'd (`instance.__dict__['__iter__']`-level shim for sync, `__aiter__` for async) or a subclass that preserves `type()`, `close()`, `__enter__/__exit__`, `__aenter__/__aexit__`, `.response`, `.text_stream`, and `with_raw_response`; `functools.wraps` on every wrapped method. Tests: `isinstance` preservation, `with` block, `close()`, `with_raw_response`, LangChain + LlamaIndex smoke, and an async cancellation that must propagate.

### F7 — M · Trigger capsules collide with the schema and with deterministic sealing (§10.5 × §9.3 × §13.3)

§10.5: a `freeze` action builds a capsule *"without closing the run"*, and *"a later crash produces a second, superset capsule — `blackbox ls` shows both"*.
But §9.3 has a single `run.capsule_path`, and §13.3 defines re-sealing as **byte-identical** and idempotent by `capsule_id`. Two capsules for one `run_id` breaks both.

**Patch:** capsule identity becomes `(run_id, seal_no)` where `seal_no` is monotonic per run and recorded in `run.json`, the capsule id is `c_<run>_<seal_no>`, and:
- §9.3: drop `run.capsule_path`; read it from `artifact` rows (`kind='capsule'`, ordered by `created_ns`). Add `seal_no INTEGER` to `artifact`.
- §13.3: state determinism as *"re-sealing `(run, seal_no)` with identical inputs yields byte-identical output"*.
- `blackbox ls` shows `capsules: 2 (trigger:loop@seq4180, crash)`.

### F8 — M · Roll preallocates 16 MiB on the writer thread (§8.6 step 2)

Preallocating 16 MiB synchronously (`posix_fallocate`, or a 16 MiB zero-fill on filesystems without it) inside the writer is a 5–50 ms stall in the middle of a provider call — exactly the p99 hazard §15 protects. The doc makes keeper pre-creation conditional on the benchmark being worse; it should be the default.

**Patch:** in §8.6, make step 2 deferred:

> When `head_off > 0.75 × segment_bytes` the keeper pre-creates and preallocates `<n+1>.seg` and sets `ROLL_PENDING`. At roll time the writer only `mmap`s a ready file (≈ 50 µs). The synchronous create path remains as the fallback when the keeper is down or `mode = volatile`, and it is the measured path in P1-17. Target: roll p99 ≤ 5 ms **with** keeper pre-creation, ≤ 50 ms without (documented difference).

### F9 — M · A frame larger than a segment is undefined (§8.3 × §8.6)

`segment_bytes` is configurable (64 KiB is a test size, 2 MiB in serverless mode) and payloads are capped at 1 MiB → a single frame can exceed `segment_bytes − 4096`. Nothing in §8.6 says what happens; the likely implementation is an infinite roll loop.

**Patch:** add to §8.6 and to §18 P1:

> `frame_max = segment_bytes − 4096 − 128`. Any payload whose encoded event would exceed it is externalized to the blob store and the event carries a ref stub (§8.8) — the same path as the 1 KiB inline threshold, just a different trigger. `install()` validates that `segment_bytes ≥ 65536` and rejects configurations where a P0 event cannot fit, with `BBX-E130`.

### F10 — L · P10 ledger arithmetic is off by 10 h (§18.12 × §22.1)

Rows sum to **82 h**, header says **72 h**. Therefore §22.1's "everything = 988 h" is really **998 h** (v1.0 = 916 h is correct: I re-summed all twelve phases).

**Patch:** set the P10 header to 82 and §22.1's total row to 998 (or drop a 10 h row).

### F11 — L · Residual 1.0 wording contradicts 1.1's own decisions

| Where | Residual | Fix |
|---|---|---|
| §7.2, `flaviorg` row, "Difference we can claim" | "ring+spill" | "segment tape with pre-allocated roll + in-flight guarantee" |
| §7.2, `stepfork` row | "retention/spill" | "retention as sealed-segment deletion + pin set" |
| §15.4 trade-off triangle | `ring_bytes ↑` | `segment_bytes ↑` (and note memory is one mapping either way) |
| §8.10 I5 | "`WRAP_PAD`" | "`SEG_END`/`GAP`" |
| §16.2 S7/S8 | `WRAP_PAD` / "eviction after GAP, before overwrite" | move to Appendix E as ring-only (S9 already is) |

### F12 — L · `head.sealed_seq` duplicates the `.seal` truth (§8.2)

A crash between `.seal` `replace` and head publish leaves `head.sealed_seq < ` the sealed range. Two sources of truth, one of which can lie.

**Patch:** §8.2: *"`sealed_seq` is a **hint** for readers that do not want to stat the directory; the `.seal` sidecars are authoritative, and recovery recomputes `sealed_seq` from them before trusting the head."*

### F13 — L · `blackbox run` silently skips the proxy when the user already set a base URL (§11.9)

*"only for variables the user has not already set … it is never overridden silently"* — correct policy, but the user then believes they are proxied and they are not.

**Patch:** print one line at start: `proxy: OPENAI_BASE_URL already set (custom) → recording that upstream instead of api.openai.com` and record `proxy_applied: partial` in `run.json`.

---

## 4. Two things 1.1 gets right that I would have gotten wrong

Worth saying explicitly, because a review that only lists defects is a bad review:

1. **Killing in-place eviction (CH-02) was the right call even though I argued for the ring.** My justification was "bounded retention with one file"; but the ring's bounded retention is exactly what forces in-place overwrite, and in-place overwrite is the only place in the design where a committed byte is destroyed. Segments cost more files and one `fsync` per 16 MiB and buy the elimination of the hardest invariant (I1) — a good trade at this stage.
2. **Serverless (CH-08) and the egress-rule correction (CH-15) are both honesty fixes, not features.** §11.11 says out loud that recovery does not cross a sandbox teardown, and CH-15 stops the document from claiming something the proxy falsifies. Those two rows do more for the product's credibility than any new capability in 1.1.

---

## 5. Sequencing reservation (the one strategic change I'd make)

1.1 orders **P3 → P3B (proxy/hooks) → P4 (capsule, CLI, viewer)**. That means the universal-attach vantage points ship *before* there is a shareable artifact, so for the whole of P3B the output of "attach to anything" is a tape nobody can hand to anyone.

**Recommendation:** run **P4 before P3B**. Reasons:
- P4 turns every tape into a capsule; then each new vantage point (proxy, hooks) is immediately demonstrable and reviewable, and the P3B acceptance tests can be "produce a capsule" instead of "produce a directory".
- P3B is the riskiest phase by far (listener lifecycle, byte-identity, redirects, SSE, TLS, port collision, hostile upstreams). If it slips, the product still has crash forensics + a viewer + a CLI, i.e. a shippable 0.1.0. In the current order, a P3B slip leaves nothing to show even though P3 and P4's work is done.
- The one thing lost is 2–3 weeks of "it attaches to my Node app" demo value. That is worth less than the option value of a shippable artifact.

Cost of the swap: zero effort, one dependency edge. If you keep 1.1's order, then add a hard gate to P3B-01: **do not start P3B until S1–S13 pass** — the same rule I applied to the storage core.

---

## 6. What I need from you before I cut v1.2

| # | Question | Default if you say nothing |
|---|---|---|
| A | Apply F1–F13 and cut `BLACKBOX_FINAL_ARCHITECTURE_v1.2.md`? | **no** — I will wait; the findings are patch-ready |
| B | Adopt the P4-before-P3B swap (§5)? | yes, with the S1–S13 gate as fallback |
| C | Seam C coverage label: add `SEMANTIC` (F5) or drop seam C and keep A/B/D? | add `SEMANTIC`; C is the fallback that survives an httpx rewrite |
| D | Pin set (F2): `pins.json` file or index-only? | file, because the index is deletable by design |
| E | Compaction (F4): keep it in v1.0 at all? | **defer to P10** — it is the only feature that can invalidate a `.seal`, and it buys ~40 % disk |

Everything above is a row-level edit; none of it reopens a design decision. v1.1 is close enough that I would start P0-01 the moment A and B are answered — the first 24 h of work (P0 + P1-01…P1-05) is unaffected by every finding in this review.

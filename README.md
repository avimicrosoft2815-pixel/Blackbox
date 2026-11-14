# BlackBox

**Status:** planning — no product code yet.

*"Attach in one command to any program that calls a model over HTTP and lets you set its base URL — in any language — plus Claude Code and Codex through their hooks. Closed consumer apps are not observable and we say so."*

## What BlackBox does not attach to (hard limits, verbatim from the authoritative plan §11.12)

| Surface | Why not | What we offer instead |
|---|---|---|
| ChatGPT desktop / web, Claude desktop / web | no client-side hook or extension API for ChatGPT desktop **[Not found in this search]**; Claude Desktop exposes only MCP | MCP read access to capsules (Claude Desktop, §19.4); import of official data exports (P12-10) |
| Consumer mobile apps | certificate pinning; one VPN at a time on Android; CA install required | none; stated |
| gRPC-only providers | seams A–D are HTTP/1.1 JSON/SSE | `blackbox.capture()` manual route; v2 decoder work |
| Request-signed APIs through the proxy (Bedrock SigV4) | the signature covers the `Host`; a forwarding proxy breaks it | in-process botocore event seam (v2, P10-09) |
| Apps that ignore base-URL settings and pin TLS | nothing short of MITM or eBPF | D14 (v2 opt-in MITM) or nothing |
| Already-running processes | no safe, portable injection without debugger privileges | restart under `blackbox run` |
| "Everything the app does" (DB, files, subprocesses) | §6.3; Keploy shows the cost (eBPF, root, per-protocol decoders) | the LLM boundary only, deliberately |

## Where the plan lives

- **Authoritative:** `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` (BBX-ARCH-1.1) — build order, byte tables, guarantees, ticket ledger.
- **Review of record:** `docs/REVIEW_v1.1_FINDINGS.md` — 13 findings, 4 blockers, awaiting resolution.
- **Superseded history:** `docs/archive/BBX-ARCH-1.0.md` — read-only.
- Nothing here is implemented, benchmarked or released. Every test in the plan is *planned* until `docs/progress.md` exists and says otherwise.

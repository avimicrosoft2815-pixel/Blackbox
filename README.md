# BlackBox

**alpha: capture, recovery and capsules work; there is no viewer yet**

A local-first flight recorder for programs that call a model over HTTP. It records a program's model calls — through a
loopback proxy, the SDK's own HTTP client, or a coding agent's hooks — into a rolling segment tape with a hash chain
and seals, and recovers a verifiable capsule when the process dies. Nothing leaves the machine: the tape is the product
and there is no telemetry.

- **What is green, pending or not testable here:** [`docs/STATUS.md`](docs/STATUS.md)
- **What is guaranteed, per failure class:** [`docs/guarantees.md`](docs/guarantees.md)
- **The tape format, byte by byte:** [`docs/format.md`](docs/format.md)
- **The plan and the ledger of record:** [`docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md`](docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md), [`docs/progress.md`](docs/progress.md)

## What works today

```console
$ python -m blackbox --version
blackbox 0.1.0.dev0 (format v2)
```

```console
$ blackbox run -- python app.py     # proxy + SDK seam + hooks, one run, recovered at exit
$ blackbox run -- node app.js       # the same, through the proxy and the hook adapters
$ blackbox hook install --claude-code   # an agent's hook events become tape frames of their own vantage
$ blackbox link <a> <b>             # declare that two tapes describe one run

$ make test     # 854 tests: codec, framing, tape, crash points, ENOSPC, proxy, seams, hooks, capsules
$ make kill     # 500 randomized parent-SIGKILL trials, then a recovery scan of every tape
$ make bench    # per-stage microbench into bench/results/ (ext4 gate column + labelled tmpfs column)
$ make fuzz     # 100k random CBOR buffers; nothing but CBORError may escape
```

The tape survives process death for anything published to `head`; kernel-crash durability needs `mode=kernel`
or `paranoid` and is measured separately. **Nothing here is a claim about power loss** — see
`docs/guarantees.md` §1 for which failure class each test covers and which one it does not.

## What this is not yet

**No viewer** (the timeline UI does not exist yet), no `doctor`/`verify`/`ls`/`capsule`/`recover` commands, no
index/graph/replay, retention and the writer lease are implemented but not yet wired into a run, and the Codex hook
schema is unverified against a real client. Every one of those is a phase in the plan; `docs/STATUS.md` says which
phase is done and `docs/progress.md` is the ticket-by-ticket ledger.

## Licence

None yet. The owner has not chosen one, so there is no `LICENSE` file and no `license` field in
`pyproject.toml`; please do not assume permission to redistribute.

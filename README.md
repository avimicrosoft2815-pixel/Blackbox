# BlackBox

**pre-alpha: storage and recovery layer only; no SDK, proxy or viewer yet**

A local-first flight recorder for programs that call a model over HTTP. This repository currently contains the
storage primitives the rest of the product is built on: a rolling segment tape, a two-slot head, a hash chain,
segment seals and a recovery scanner — with the crash behaviour tested, not asserted.

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
$ make test     # 157 tests: codec, framing, tape, adoption, crash points, ENOSPC, keeper, forbidden words
$ make kill     # 500 randomized parent-SIGKILL trials, then a recovery scan of every tape
$ make bench    # per-stage microbench into bench/results/ (ext4 gate column + labelled tmpfs column)
$ make fuzz     # 100k random CBOR buffers; nothing but CBORError may escape
```

The tape survives process death for anything published to `head`; kernel-crash durability needs `mode=kernel`
or `paranoid` and is measured separately. **Nothing here is a claim about power loss** — see
`docs/guarantees.md` §1 for which failure class each test covers and which one it does not.

## What this is not yet

No capture API, no proxy, no viewer, no CLI beyond `--version`/`status`, no database, no retention, no capsules.
Every one of those is a phase in the plan, and `docs/STATUS.md` says which phase is done.

## Licence

None yet. The owner has not chosen one, so there is no `LICENSE` file and no `license` field in
`pyproject.toml`; please do not assume permission to redistribute.

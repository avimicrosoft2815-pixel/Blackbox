# Decisions (append-only)

Plan defaults are taken unless a row here says otherwise. Each row names the plan section it interprets.

| # | date | decision | plan ref | why |
|---|---|---|---|---|
| D-001 | 2026-11-14 | `requires-python >= 3.11`; build backend setuptools; console script deferred to P4-05 | P0-01 | 3.10 is EOL Oct 2026; `tomllib` avoids a dependency; hatchling would require an install |
| D-002 | 2026-11-14 | Dev-only tools: `pytest`, `hypothesis`, `ruff`, `mypy`, `coverage` are allowed by brief Part B §2 and are installed in `.venv` only (C6); the shipped package still has zero runtime dependencies | §3 | `ruff check` and `mypy --strict` are part of `make fmt` / `make test-slow`; the core is stdlib-only, no dependency is ever added to `pyproject.toml` for them |
| D-003 | 2026-11-14 | F8 re-scoped: keeper pre-creation of `<n+1>.seg` is the default; the synchronous roll path is measured per OS | §8.6 | owner instruction |
| D-004 | 2026-11-14 | Event-kind and error-code registries live in `constants.py` / `errors.py`; only the lead edits them | §8.4, §19 | contract files |
| D-005 | 2026-11-14 | The `head` file is `mmap`'d once at `open()`; publish is `struct.pack_into` into the alternate slot | §8.2 step 7 | "no syscall at all (page cache)" is the plan's own text |
| D-006 | 2026-11-14 | A magic that is shorter than its field is padded with `0x00` and frozen in hex in `docs/format.md` | §8.3 | A7 |
| D-007 | 2026-11-14 | **Format freeze, not a default:** the only checksum in format v2 is CRC-32/ISO-HDLC (`zlib.crc32`, check value `0xCBF43926`) | §8.3, §8.7, P1-01 | pure-Python CRC32C measured 110.5 us/KiB vs 0.45 us/KiB and would break the §15.1 event budget alone; `meta.flags` bit 2 is reserved and a reader rejects a tape that sets it (`BBX-E221`). Recorded as DV-005 / ADR-004 |
| D-008 | 2026-11-14 | Default group-commit threshold is 16 KiB (`DEFAULT_BATCH_BYTES`) | §8.2, §15.1 | amplification 1.231x at 16 KiB vs 1.843x with a commit per event; the §15.1 row is ≤1.25x |
| D-009 | 2026-11-14 | The progress ledger is generated from the plan by `tools/gen_progress.py` | A1 | a ledger that can drift from the plan is worse than none |

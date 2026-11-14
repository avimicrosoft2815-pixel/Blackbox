# Decisions (append-only)

Plan defaults are taken unless a row here says otherwise. Each row names the plan section it interprets.

| # | date | decision | plan ref | why |
|---|---|---|---|---|
| D-001 | 2026-11-14 | `requires-python >= 3.11`; build backend setuptools; console script deferred to P4-05 | P0-01 | 3.10 is EOL Oct 2026; `tomllib` avoids a dependency; hatchling would require an install |
| D-002 | 2026-11-14 | Dev-only tools: `pytest`, `hypothesis` (installed here); `ruff`, `mypy`, `coverage` allowed by the brief but **not installed** in this sandbox | §3 | no other installs; `ruff`/`mypy` runs are reported as `not testable in this environment` until they exist |
| D-003 | 2026-11-14 | F8 re-scoped: keeper pre-creation of `<n+1>.seg` is the default; the synchronous roll path is measured per OS | §8.6 | owner instruction |
| D-004 | 2026-11-14 | Event-kind and error-code registries live in `constants.py` / `errors.py`; only the lead edits them | §8.4, §19 | contract files |
| D-005 | 2026-11-14 | The `head` file is `mmap`'d once at `open()`; publish is `struct.pack_into` into the alternate slot | §8.2 step 7 | "no syscall at all (page cache)" is the plan's own text |
| D-006 | 2026-11-14 | A magic that is shorter than its field is padded with `0x00` and frozen in hex in `docs/format.md` | §8.3 | A7 |

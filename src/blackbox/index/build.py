"""Building the index, and the canonical dump that makes I8 checkable.

I8 says the index is a **pure function** of its sources: `rebuild` — which throws the file away and starts from nothing —
must produce the same rows, in the same order, from the same capsules as an index that was built one capsule at a time.
`canonical_dump` is the comparable form: the four source tables, every row, ordered by primary key, rendered as one JSON
line each, with paths reduced to file names and no timing in sight. Its sha256 is the thing a test (or a person) compares.

Determinism has three ingredients, and each is a place this file could have been wrong:

* sorted capsule discovery (`sorted(glob(...))`), never `os.listdir` order;
* no wall-clock, pid or absolute path in the compared tables;
* `ON CONFLICT DO NOTHING` everywhere, so the second capsule of a two-tape run cannot overwrite the first one's row.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import time
from typing import Any

from blackbox.index import ingest, schema


def capsules(data_dir: str | os.PathLike[str]) -> list[str]:
    """Every capsule in the data directory, in the one order the build may use."""
    return sorted(glob.glob(os.path.join(str(data_dir), "capsules", "*.bbx")))


def build(data_dir: str | os.PathLike[str], *, incremental: bool = True) -> dict[str, Any]:
    """Index every capsule. `incremental=True` keeps an existing index and re-ingests (idempotent); False rebuilds."""
    started = time.perf_counter()
    if not incremental:
        shutil.rmtree(os.path.join(str(data_dir), schema.INDEX_DIR), ignore_errors=True)
    con = schema.connect(data_dir, create=True)
    try:
        con.execute("INSERT INTO index_meta (k, v) VALUES ('built_at_ns', ?) "
                    "ON CONFLICT (k) DO UPDATE SET v = excluded.v", (str(time.time_ns()),))
        unreadable: list[dict[str, str]] = []
        disagreements: list[str] = []
        paths = capsules(data_dir)
        added = 0
        # One transaction for the whole build. Each capsule used to commit its own rows, which is 500 journal writes and
        # 500 syncs for the acceptance corpus — the cost that put this test past the 120 s watchdog on Windows runners
        # (defect 114). The index is *derived*: the capsules are the durable thing, so losing a half-built index to a crash
        # costs one rebuild and no evidence. `capsule_rows` reads files and never writes, so it stays outside.
        con.execute("BEGIN IMMEDIATE")
        try:
            for path in paths:
                try:
                    rows = ingest.capsule_rows(path)
                except Exception as exc:             # one broken capsule must not stop the index
                    unreadable.append({"capsule": os.path.basename(path), "error": f"{type(exc).__name__}: {exc}"})
                    continue
                disagreements.extend(rows.pop("disagreements"))
                added += ingest.insert_rows(con, rows)["added"]
            con.execute("INSERT INTO index_meta (k, v) VALUES ('last_rebuild_ns', ?) "
                        "ON CONFLICT (k) DO UPDATE SET v = excluded.v", (str(time.time_ns()),))
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        counts = schema.count_rows(con)
    finally:
        con.close()
    return {
        "data_dir": str(data_dir),
        "path": str(schema.main_path(data_dir)),
        "mode": "incremental" if incremental else "rebuild",
        "capsules_seen": len(paths),
        "rows_added": added,
        "counts": counts,
        "unreadable": unreadable,
        "disagreements": disagreements,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def rebuild(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Throw the index away and build it again from the capsules. What `blackbox index --rebuild` runs."""
    return build(data_dir, incremental=False)


def canonical_dump(data_dir: str | os.PathLike[str]) -> bytes:
    """The comparable form of the index: rows, order, nothing that moves. `index_meta` is excluded by design."""
    con = schema.connect(data_dir)
    try:
        lines: list[str] = [json.dumps({"schema_version": schema.schema_version(con)}, sort_keys=True)]
        for table, order in schema.DUMP_TABLES:
            # `table`/`order` are `schema.DUMP_TABLES` — module constants, never a caller's string.
            for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}"):  # noqa: S608
                lines.append(json.dumps({table: dict(row)}, sort_keys=True, ensure_ascii=False))
    finally:
        con.close()
    return ("\n".join(lines) + "\n").encode("utf-8")


def dump_hash(data_dir: str | os.PathLike[str]) -> str:
    return hashlib.sha256(canonical_dump(data_dir)).hexdigest()

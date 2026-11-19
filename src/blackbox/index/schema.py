"""The data-directory index: one SQLite file, five tables, no third-party code (P5-06, §9).

The per-run `index.sqlite` under `data/runs/<key>/` belongs to one recovery and dies with it. This is the other index:
`data/index/main.sqlite`, the whole data directory's answer to "which runs have I got, what happened in them, and what
evidence backs that", built by reading **only** the sealed capsules.

Two design points carry the property the plan asks for (I8, *the index is a pure function of its sources*):

* **Ingest is idempotent by primary key** (`INSERT ... ON CONFLICT DO NOTHING`), so ingesting the same capsule twice is
  the same as ingesting it once and re-ingesting a run is always safe (§9.4).
* **Nothing that changes between builds is stored inside the compared tables.** There is no "built at" column, no
  absolute path and no iteration order: capsule paths are stored as file names, rows are inserted in sorted capsule
  order, and timings live in `index_meta`, which a canonical dump excludes except for `schema_version`.

`STRICT` tables catch a type bug at insert time — a `t_wall_ns` that arrived as text would otherwise sort as a string
and break every range scan. SQLite grew `STRICT` in 3.37, so a build on an older library falls back to the same DDL
without it and records `schema_strict=0` in `index_meta`; the dump reports that key, so a comparison across two
different builds cannot silently compare two different schemas.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
INDEX_DIR = "index"
INDEX_NAME = "main.sqlite"

_TABLES_STRICT = (
    """CREATE TABLE IF NOT EXISTS run (
           run_id TEXT PRIMARY KEY, capsule TEXT NOT NULL, root TEXT, status TEXT, status_cause TEXT,
           completeness TEXT, events INTEGER NOT NULL, nodes INTEGER NOT NULL, bytes INTEGER NOT NULL,
           verified INTEGER NOT NULL DEFAULT 0
       ) WITHOUT ROWID, STRICT""",
    """CREATE TABLE IF NOT EXISTS node (
           run_id TEXT NOT NULL, node_slot INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
           status TEXT, PRIMARY KEY (run_id, node_slot)
       ) WITHOUT ROWID, STRICT""",
    """CREATE TABLE IF NOT EXISTS event (
           run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind INTEGER NOT NULL, t_wall_ns INTEGER NOT NULL,
           bytes INTEGER NOT NULL, PRIMARY KEY (run_id, seq)
       ) WITHOUT ROWID, STRICT""",
    """CREATE TABLE IF NOT EXISTS artifact (
           artifact_id TEXT PRIMARY KEY, run_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL, bytes INTEGER,
           sha256 TEXT, created_ns INTEGER
       ) STRICT""",
    """CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID, STRICT""",
)

_TABLES_PLAIN = tuple(
    statement.replace(" WITHOUT ROWID, STRICT", "").replace(", STRICT", "").replace(" STRICT", "")
    for statement in _TABLES_STRICT
)

#: The tables a canonical dump covers, in the order it covers them, with the primary-key ordering that makes the dump
#: independent of insertion order. `index_meta` is deliberately absent — it holds timings.
DUMP_TABLES: tuple[tuple[str, str], ...] = (
    ("run", "run_id"),
    ("node", "run_id, node_slot"),
    ("event", "run_id, seq"),
    ("artifact", "artifact_id"),
)


def main_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / INDEX_DIR / INDEX_NAME


def connect(data_dir: str | os.PathLike[str], *, create: bool = False) -> sqlite3.Connection:
    """Open the index. `create=True` makes the directory and schema; otherwise a missing index is an error."""
    path = main_path(data_dir)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(str(path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")          # no cross-table FKs by design (§9.5: enforced in code)
    con.execute("PRAGMA synchronous = NORMAL")
    if create:
        create_schema(con)
    return con


#: `STRICT` and `WITHOUT ROWID` arrived in SQLite 3.37 (2021-11-27). Older libraries are rare on a machine that runs
#: Python 3.11, but "rare" is not "impossible" and a build that silently fell back without saying so would compare two
#: different schemas and call it I8.
STRICT_MIN_VERSION = (3, 37, 0)


def supports_strict() -> bool:
    return sqlite3.sqlite_version_info >= STRICT_MIN_VERSION


def create_schema(con: sqlite3.Connection) -> bool:
    """Create the five tables if they are not there. Returns whether this library's tables are `STRICT`."""
    strict = supports_strict()
    for statement in (_TABLES_STRICT if strict else _TABLES_PLAIN):
        con.execute(statement)
    con.execute("INSERT INTO index_meta (k, v) VALUES ('schema_version', ?) "
                "ON CONFLICT (k) DO NOTHING", (str(SCHEMA_VERSION),))
    con.execute("INSERT INTO index_meta (k, v) VALUES ('schema_strict', ?) "
                "ON CONFLICT (k) DO NOTHING", ("1" if strict else "0",))
    return strict


def schema_version(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT v FROM index_meta WHERE k = 'schema_version'").fetchone()
    if row is None:
        return 0
    return int(row["v"])


#: Every table the index keeps, in insert order. `artifact` last because it names capsules the other rows came from.
TABLES: tuple[str, ...] = ("run", "node", "event", "artifact")


def count_rows(con: sqlite3.Connection) -> dict[str, int]:
    """Rows per table. Table names come from `TABLES`, a module constant — never from a caller or a file."""
    return {name: int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608 - constant, not input
            for name in TABLES}


def meta(con: sqlite3.Connection) -> dict[str, Any]:
    return {row["k"]: row["v"] for row in con.execute("SELECT k, v FROM index_meta ORDER BY k")}

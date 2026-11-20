"""The data-directory index (P5-06, §9): what runs exist, what happened in them, and I8 by construction.

`build_index` reads sealed capsules into `data/index/main.sqlite`; `rebuild_index` throws that file away and does it
again; `canonical_dump` is the form in which the two can be compared. The per-run `index.sqlite` inside a recovery's work
directory is a different artefact and is not this.

The exports are named `build_index`/`rebuild_index` rather than `build` because a package attribute that shadows its own
submodule (`blackbox.index.build`) turns `import blackbox.index.build as x` into a function — a trap this branch walked
into once while writing the tests.
"""
from blackbox.index.build import build as build_index
from blackbox.index.build import canonical_dump, capsules, dump_hash
from blackbox.index.build import rebuild as rebuild_index
from blackbox.index.schema import INDEX_NAME, SCHEMA_VERSION, main_path

__all__ = ["INDEX_NAME", "SCHEMA_VERSION", "build_index", "canonical_dump", "capsules", "dump_hash", "main_path",
           "rebuild_index"]

"""`blackbox link <a> <b>` — §11.10's *declared* link, recorded and then re-resolved.

The three link classes exist so that a reader can tell a measurement from a statement. `exact` is a measurement (the
same run id arrived on both sides through `x-blackbox-run`), `inferred` is an inference with its evidence in view, and
`declared` is this command: a human saying "these two tapes describe one run". So the command does two things and says
which is which:

1. **Records the statement.** The pair is appended to `<data_dir>/links.declared.json` in *both* directories, so that a
   capsule prepared from either side can see it — a declaration that only one side remembers is a declaration the other
   side's evidence contradicts. Recording is idempotent: running the command twice leaves one assertion, not two.
2. **Prints the resolved graph.** Everything the tapes now support between them, with classes, evidence and confidence,
   so the user sees whether their statement was enough or whether other links already existed. The exit code is 0 when
   the declaration was recorded, whatever it resolved to: the link *is* the answer and an empty graph still honours it.

What the command refuses, and why: a directory with no readable tape, a tape that does not name its run, and a
directory naming more than one run. Each of those means the declared pair would be recorded between two things that are
not identifiable, and a link between `None` and `None` is worse than no link at all.
"""
from __future__ import annotations

import argparse
import sys

from blackbox.rec import correlate, links


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="blackbox link",
                                 description="Declare that two tapes describe one run (§11.10), then show the graph")
    ap.add_argument("left", help="a data directory (the one holding <data_dir>/tape)")
    ap.add_argument("right", help="the other data directory")
    ap.add_argument("--window-ms", type=int, default=links.DEFAULT_WINDOW_NS // 1_000_000,
                    help="the inference window in milliseconds (default: 2000, §11.10)")
    return ap.parse_args(argv)


def _run_id_of(directory: str) -> str:
    """The single run id this directory's tapes name — or an exception saying why there is not one."""
    named = sorted({v.run_id for v in correlate.views(directory) if v.run_id})
    if not named:
        raise ValueError(f"{directory}: no tape names its run (no RUN_START and no linked_run), so there is nothing "
                         f"to declare; pass --run to the writer that made it")
    if len(named) > 1:
        raise ValueError(f"{directory}: {len(named)} run ids among its tapes ({', '.join(named)}); "
                         f"a declared link names one run per side")
    return named[0]


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    window_ns = max(1, args.window_ms) * 1_000_000
    try:
        left_run = _run_id_of(args.left)
        right_run = _run_id_of(args.right)
    except ValueError as exc:
        print(f"blackbox link: {exc}", file=sys.stderr)
        return 2
    pair = correlate.record_declared(args.left, left_run, right_run)
    if args.left != args.right:
        correlate.record_declared(args.right, left_run, right_run)
    records = correlate.resolve_across([args.left, args.right], window_ns=window_ns)
    document = correlate.document(records, window_ns=window_ns,
                                 extra={"declared": pair, "directories": [args.left, args.right]})
    sys.stdout.buffer.write(document)
    sys.stdout.flush()
    return 0

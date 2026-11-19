# `bbx-read` — a Go reader for `.bbx` capsules, written from `docs/format.md`

This is the reader P11-15 asks for: a second implementation in a different language, written from the document rather
than from `src/blackbox/cap/format.py`, whose job is to answer a question one implementation cannot answer about itself —
**is the document enough to read a capsule?**

It exists in the tree, and it has **not been compiled or run on the machine this branch was built on**: there is no Go
toolchain there. It is compiled and executed by the `go-reader` CI job on every push
(`sh tools/readers/go/selftest.sh`), which builds it, reproduces the seven CRC-32 conformance vectors, verifies a capsule
written by the Python implementation, and expects three named refusals (a tampered entry → exit 4; a non-ZIP file → exit
3; a traversal entry → exit 3).

Until that cell is green on a commit, the honest reading of this directory is **written and reviewed, not yet run**.
`tests/test_go_reader.py` checks everything that can be checked without a compiler — stdlib-only imports, refusal codes
that are in the registry, the selftest's shell syntax, and that CI actually runs it — and checks nothing about whether the
Go is correct.

## What it checks

| Step | Rule from the document |
|---|---|
| names | absolute, `..`, drive letter, backslash, NUL and empty components refused (`BBX-E403`) — §8.1, §13.4 |
| limits | 5 000 entries, 4 GiB total, 100:1 expansion, encrypted or non-STORED/DEFLATE members refused (`BBX-E402`, `E405`, `E406`) |
| duplicates | case-folded duplicate names refused (`BBX-E404`) |
| entries | each read once, bounded by its declared size (`BBX-E409` when it lies) |
| `INTEGRITY.sha256` | `<64 lower-case hex>  <name>\n`, verified entry by entry |
| root | `manifest.integrity.root == sha256(INTEGRITY.sha256)` |
| frames | `BBXSEG2` magic, segment-header CRC over `[0,96)`, `BBXF` frames with `BBXT` trailers, the frame CRC over `header[0,44) ‖ payload ‖ padding ‖ trailer` |
| counts | `manifest.counts.events` compared with the frames actually decoded |

It does **not** re-render `derived_parts` (that needs the exporters), and it does not check semantic claims that are not
a hash or a count.

## Running it

```console
$ sh tools/readers/go/selftest.sh                       # the CI job, exactly
$ cd tools/readers/go && go build -o /tmp/bbx-read . && /tmp/bbx-read <capsule.bbx> --json
$ /tmp/bbx-read --vectors tests/conformance/vectors/crc32.json
```

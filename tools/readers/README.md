# Independent readers

Code in this directory reads BlackBox's on-disk formats **without importing BlackBox**. Its purpose is to test the
documents rather than the code: if a reader written from `docs/format.md` alone cannot decode a real capsule, the
document is missing something, and that is a defect in the document — not in the reader.

| Reader | Language | Reads | Written from |
|---|---|---|---|
| `bbx-read.mjs` | Node ≥ 18, no dependencies | `.bbx` capsules: zip structure, `INTEGRITY.sha256` and its root, `manifest.json`, and the frames inside `events/*.raw` | `docs/format.md` §4, §5, §8.1 and the `.bbx` refusal table in `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §13.4 |

```
node tools/readers/bbx-read.mjs capsule.bbx            # a summary, exit 0 verified / 3 refused / 4 root mismatch
node tools/readers/bbx-read.mjs capsule.bbx --json     # the same as JSON
node tools/readers/bbx-read.mjs --vectors tests/conformance/vectors/crc32.json
```

The imports are `node:fs`, `node:zlib` and `node:crypto` and nothing else, and the test that asserts that
(`tests/test_independent_reader.py::test_the_reader_imports_nothing_from_this_project`) also runs the reader with an
empty environment so a `PYTHONPATH` accident cannot make it look independent when it is not.

## What the two implementations agree on, as of 2026-11-19

`tests/test_independent_reader.py` (16 tests) checks three different claims:

* **Agreement on a real capsule** — a tape recorded and sealed by the recovery state machine: same entries, same
  integrity root (computed twice, from two codebases), same frame count as `manifest.counts.events`, same kinds.
* **Agreement on refusals** — eight hostile capsules plus an encrypted member and a non-zip file: both readers refuse
  with the same `BBX-Exxx` (E402, E403, E404, E405, E406, E407, E408).
* **Agreement on severity** — a manifest whose `integrity.root` does not match its own bytes is exit 4 with the reason
  named by the Node reader and `root_ok: false` by the Python one. Neither implementation treats it as unreadable.

## What writing it found (defect 85)

Three places where the document was not enough, all now amended in `docs/format.md`:

1. §4 put the frame trailer at `48 + payload_len` in its table while the writer puts it at `frame_len - 8` (the padding
   travels with the payload, before the trailer). A reader that followed the table failed on the first real frame.
2. §8.1 said the manifest carries "identity" without naming the values: `format` is `bbx.capsule`, `format_version` is
   `0`. They were read off a capsule and are now written down.
3. §8.1 did not state `INTEGRITY.sha256`'s line format (64 hex, two spaces, name, `LF`, sorted), and
   `manifest.integrity.entries` counts *all* members — a reader that assumes it counts lines is off by two.

§5 described the segment header's own checksum as "profile" without naming the function; it is CRC-32/ISO-HDLC over
bytes `[0, 96)`, and it is now stated, so the reader verifies it.

## What this is not

It is not the Go reader `P11-15` asks for: there is no Go toolchain on the machine this was built on, and the plan names
Go. Node was the second language available. A Go reader written from the same document remains open, and the two
documents now carry the facts it needs.

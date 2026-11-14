# Deviations (append-only)

A deviation is only recorded when it cannot be avoided; the plan and `docs/format.md` change in the same commit.

| # | date | finding / reason | plan section | change | why it is safe |
|---|---|---|---|---|---|
| DV-001 | 2026-11-14 | F4 (review finding) | §8.7 | `.seal` `body_len`, `body_crc32c`, `body_hash` are defined over the **uncompressed** body; the codec lives in the segment header | byte layout unchanged; approved as confined to P1-13 |
| DV-002 | 2026-11-14 | the segment magic in §8.7 is 7 characters in an 8-byte field | §8.7 | `42 42 58 53 45 47 32 00` — `BBXSEG2` + NUL pad; all four magics now stated in hex | fixes an ambiguity in the plan, no behaviour change |
| DV-003 | 2026-11-14 | `host_fingerprint` used `socket.gethostname()`, which P0-14 forbids outside the proxy | §8.2 `0x048` | `platform.node()` — same OS hostname, no `socket` import, no resolver | the field still hashes hostname+os+machine |
| DV-004 | 2026-11-14 | `BBX-E140` was reused for "segment preallocation failed" while the registry/plan define it as a proxy allowlist error | §19 | preallocation/orphan-segment cases use `BBX-E133`; registry compared against the plan appendix by a test | removes a collision |

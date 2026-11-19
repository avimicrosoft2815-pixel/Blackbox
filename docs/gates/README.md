# Gate files

One file per gate, named `G-<name>.md`, written **by the reviewer** — never by the builder — after the reviewer has
cloned the candidate commit fresh, run `make test`, `make kill` (short) and `make bench` smoke, and re-read every
must-be-true row for that gate. `docs/STATE.json` flips `gates[G-<name>]` to `pass` only on the strength of a signed
file here, and `docs/STATUS.md` names the gate in its §10 header line.

## Shape (AGENT_FINAL.md §4)

```md
# G-<name> — <candidate commit>

**Reviewer** <model family / fresh context> · **date** <date> · **tree** <commit sha, pushed>

## What I ran
- `make test` → <result>
- `make kill` → <trials, violations>
- `make bench` smoke → <result>

## Must-be-true rows I re-read
| # | row | verdict | evidence I looked at |
|---|---|---|---|

## Findings
<numbered; each with the file:line or the number that shows it>

## Verdict
<pass | fail, with the one thing that would change it>

**Signed:** <reviewer> — <commit>
```

## `READINESS.md` is the builder's index, not a gate file

`docs/gates/READINESS.md` walks `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §24 and §24a row by row and says where the
evidence for each row sits in the tree, or that none was found. It is written **by the builder**, it carries no verdict,
and it cannot tick anything: rules 1 and 2 below still decide what a gate file is. Its purpose is to keep anyone from
reading a short blocker list as a long gate list.

## Rules

1. The builder may not tick its own gate. A gate file signed by the builder is void.
2. `make test` from a clean clone is mandatory: a green run in the author's working tree is not evidence.
3. A gate whose rows are labelled `requires humans` or `requires time` records that label, and the gate stays `pass`
   only for the part a reviewer can verify here.
4. Numbers in a gate file are quoted from `bench/results/*.json`, with the path — never from memory or from STATUS.
5. A failed gate names the single thing that would change the verdict.

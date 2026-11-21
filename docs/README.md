# docs/ — how this repository is organised

1. **Authoritative plan:** `BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` (BBX-ARCH-1.1). If anything disagrees with it, it wins.
2. **Reading order for a reviewer:** 1.1 §0.4 (change ledger) → §0.5 (critique → status) → §3 (decisions) → §8 and §9 (storage + database engines) → §11.7–§11.12 (attach matrix) → §18 (ticket ledger).
3. **Review of record:** `REVIEW_v1.1_FINDINGS.md` — 13 findings (F1–F4 blockers, all patch-ready, none applied).
4. **Superseded:** `archive/BBX-ARCH-1.0.md` — kept for the review trail. Do not implement from it.
5. **Missing, requested from the owner:** `BLACKBOX_ROUND1_PRIOR_ART_CRITIQUE.md` (C1–C15). Not in the repo and not in the attachments; it is referenced by 1.1 §0.4/§0.5, so it cannot be reconstructed by guesswork.
6. **All tests are planned, not executed,** until `docs/progress.md` exists and records a ticket, a commit, a test path and a result per row.
7. **Formats are frozen by `docs/format.md` first** (P0-05); a byte-table deviation requires the doc change in the same commit (execution brief §1 rule 9).
8. **Every number in a report must be measured in-session** with command, hardware, N and p50/p95/p99, stored in `bench/results/<date>-<name>.json`; otherwise it is labelled *target (unmeasured)*.
9. **Forbidden words** in code, comments, docs and commit messages are listed once, in `tests/test_forbidden_words.py` (the brief's rule 4). That test enforces them over the whole tree *and* over `git log`; if you need the list, read the test, not this file.
## Start here, by what you are trying to do

| If you want to | Read |
|---|---|
| start from nothing and finish one incident, in order | **[getting-started.md](getting-started.md)** — eight steps, each with what you should see and what to do when it goes wrong |
| install it and record something in five minutes | `onboarding.md`, then `desktop.md` for the window |
| know what it will and will not do | `limitations.md`, then `guarantees.md` |
| fix something that went wrong | `troubleshooting.md`, then `errors.md` for the code |
| know what is private | `privacy.md` |
| write against it | `cli.md` (the command contract), `format.md` (the bytes), `attach.md` (which SDK builds are verified) |
| understand why a number is what it is | `STATUS.md` (measurements and their JSON), `bench/results/` |
| review it for release | `gates/README.md`, `gates/READINESS.md`, `gates/G-Capture.md`, `RELEASE-BLOCKERS.md`, `RELEASE-CANDIDATE.md` |
| see the demo rather than the product | `../demo/blackbox-film/docs/showcase.md` |

`errors.md` is generated from `src/blackbox/errors.py` (`python tools/gen_errors_doc.py`), so a code in a terminal and a
row here cannot disagree. `tests/test_error_advice.py` asserts that every command those remediations name exists — the
`blackbox doctor` advice was dangling until the command existed (defect 111).

10. **Stop-and-wait is retired** (A1–A9 were answered); `docs/STATUS.md` is the live state and is printed verbatim whenever work stops.

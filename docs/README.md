# docs/ — how this repository is organised

1. **Authoritative plan:** `BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` (BBX-ARCH-1.1). If anything disagrees with it, it wins.
2. **Reading order for a reviewer:** 1.1 §0.4 (change ledger) → §0.5 (critique → status) → §3 (decisions) → §8 and §9 (storage + database engines) → §11.7–§11.12 (attach matrix) → §18 (ticket ledger).
3. **Review of record:** `REVIEW_v1.1_FINDINGS.md` — 13 findings (F1–F4 blockers, all patch-ready, none applied).
4. **Superseded:** `archive/BBX-ARCH-1.0.md` — kept for the review trail. Do not implement from it.
5. **Missing, requested from the owner:** `BLACKBOX_ROUND1_PRIOR_ART_CRITIQUE.md` (C1–C15). Not in the repo and not in the attachments; it is referenced by 1.1 §0.4/§0.5, so it cannot be reconstructed by guesswork.
6. **All tests are planned, not executed,** until `docs/progress.md` exists and records a ticket, a commit, a test path and a result per row.
7. **Formats are frozen by `docs/format.md` first** (P0-05); a byte-table deviation requires the doc change in the same commit (execution brief §1 rule 9).
8. **Every number in a report must be measured in-session** with command, hardware, N and p50/p95/p99, stored in `bench/results/<date>-<name>.json`; otherwise it is labelled *target (unmeasured)*.
9. **Forbidden words** in code, comments, docs and commit messages: zero loss, crash-safe, power-loss safe, secure, forensic-grade, deterministic, first ever, unique, never done before (execution brief §1 rule 4).
10. **No product code** is written until the owner replies `APPROVE STAGE 1` (execution brief §3, stop-and-wait).

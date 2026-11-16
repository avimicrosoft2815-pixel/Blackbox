"""The repair workflow: a proposal is reviewed, approved by token, applied atomically, and verified by command.

Read-only until `--approve` matches a token bound to the proposal, the repository and every target's current hash. The
product never proposes: `diagnose` writes evidence, a human or an agent writes the patch, and this package is the gate
between them. Suggested tests are displayed, never executed — only commands a human types into `repair verify` run, and
the report says which ones did.
"""
from blackbox.repair.apply import Applied, verify
from blackbox.repair.apply import apply as apply_proposal
from blackbox.repair.proposal import Proposal, ProposalError, assessed_risk, load

__all__ = ["Applied", "Proposal", "ProposalError", "apply_proposal", "assessed_risk", "load", "verify"]

"""The diagnosis workflow: evidence in, an incident report and a repair brief out, and nothing else.

Read-only by construction. The public surface is `load` (bounded, defensive), `findings` (ranked, with alternatives),
`build` (the two documents) and the `diagnose` command. There is no apply, no patch, no network call and no subprocess:
a brief is a document a human approves in their own repository, which is the part this tool does not do.
"""
from blackbox.diagnose.evidence import Evidence, EvidenceError, load
from blackbox.diagnose.findings import Finding, findings
from blackbox.diagnose.report import Report, build

__all__ = ["Evidence", "EvidenceError", "Finding", "Report", "build", "findings", "load"]

# Privacy — what leaves this machine, what is written, and how to delete it

This page is short on purpose. Every sentence is either a property the test suite asserts or a limit that
`docs/RELEASE-BLOCKERS.md` keeps open; where a claim is unmeasured, it says so.

## Nothing leaves the machine

* **No account, no cloud, no telemetry, no update check.** The recorder makes no network request of its own. There are
  two layers of enforcement, and both are tests rather than intentions:
  `tests/test_no_egress.py` (static: no socket imports or URL literals in `src/`) and `tests/test_netguard_reads.py`
  (runtime: a guard over `connect`, `create_connection`, `getaddrinfo` and `HTTPConnection.connect` while `verify`,
  `view`, `diagnose`, `ls`, `serve`, `desktop --selftest` and a recorded `run` all complete with zero attempts).
* **The proxy is the one exception, and it is the point of the tool.** `blackbox run` forwards your program's model
  traffic to the upstream you named, through a loopback listener whose route table *is* the egress allowlist: an address
  you did not configure is refused (`BBX-E140`), not forwarded.
* **No AI service is called on your behalf.** Nothing in this repository sends a capsule, a brief or a prompt to another
  model, and no command makes a network request to do its work. The repair brief is text you hand to whatever you choose.

## What is recorded

Under your content policy (`full`, `hash`, `none` — `docs/guarantees.md`):

* **Recorded:** model requests and responses as the wire carried them (or their fingerprints, in `hash` mode), stream
  chunks including a stream that was cut short, the node tree your code opens, tool calls and errors, timings, and every
  gap the recorder knows about.
* **Not recorded:** other programs' traffic, browser AI sites (this build records SDK and HTTP clients, not a browser
  extension), the contents of your source files, and anything at all when capture fails — a recorder that cannot write
  goes quiet rather than substituting a guess (`tests/test_failopen.py`).

**Secrets are redacted before serialisation**, by pattern, entropy and a deny-list, and the claim is measured on a corpus
of 60 secrets across 10 formats through **eleven** sinks — the tape's frames, the capsule, `otlp/spans.json`,
`otlp/spans.pb`, `events/events.jsonl`, `graph.json`, `manifest.json`, the viewer page, the diagnosis report, the repair
brief and the desktop's pages (`tests/test_redaction_every_sink.py`, with a redaction-off control proving the artefacts
would otherwise carry all 60). **What that does not promise:** a secret shape the corpus does not contain may survive.
That is the honest limit of every pattern-based redaction, it is why `hash` mode exists, and it is stated in
`docs/guarantees.md` rather than discovered later.

## Where it is written, and who can read it

The data directory is `$BLACKBOX_DATA_DIR`, else `$BLACKBOX_HOME`, else the platform default. At startup the recorder
creates it `0700` and every file inside `0600` on POSIX, and audits the ACL on Windows; `tests/test_permissions.py`
measures every path a real run writes *after* the run. Writing that test found four paths that were `0644`/`0755`
(defect 96).

The desktop app binds `127.0.0.1` on a kernel-chosen port under a secret path generated per process, and stops the server
when the window closes. Nothing is reachable from another machine, no profile is touched, and a `GET` cannot write
(`tests/test_ui_safety.py` hashes the data directory before and after every page load).

## Deleting it

```sh
blackbox ls                  # what is there, and how big
blackbox retention --json    # one row per segment with the reason it would go or stay — deletes nothing
```

**`retention` never deletes.** A deletion has to commit a `GAP` frame first, and only the tape's writer can commit one;
a tool that unlinked a segment without it would leave a tape that looks whole and is not (`docs/cli.md` says the same
sentence, and `tests/test_retention_dry_run.py` proves the plan writes nothing).

Your recordings are not the package's: `apt remove`, the Windows uninstaller and `pip uninstall` leave the data directory
where it is. That is deliberate for a recorder — the evidence outlives the program — and it is printed rather than
discovered.

## What a report needs, and what it must not carry

`docs/troubleshooting.md` ends with the list. The rule that matters here: **a capsule holds recorded prompts and
completions.** Sending one to somebody is your decision, not a side effect of asking for help, and nothing in the product
makes it for you.

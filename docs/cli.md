# The command line, command by command

The gate asks for two properties that are easy to *say* and easy to break: **every command has a machine-readable form**
and **every command documents its exit code**. This page is the contract, one row per command, and
`tests/test_cli_contract.py` checks it against the binary rather than against this table: the command set here must equal
the front door's, every `--json` column marked `--json` must be accepted by that command's parser, and the exit codes
below are exercised — a command that returns `1` where this page promised `2` fails the suite.

Two conventions run through all of it:

* **exit codes.** `0` means the command did what it was asked. `2` is *bad input or refusal*: a path that is not there, a
  proposal that fails validation, a usage error. Anything else is a finding the command produced, and it is always the
  same number for the same finding, so a script does not have to parse English.
* **`--json`.** Where a command prints a human sentence, `--json` prints one object on **stdout** instead, and every
  warning still goes to **stderr**. `nothing` means the command has no output to make machine-readable (it writes a file
  or nothing at all), and that is stated rather than papered over with an empty `{}`.

| Command | What it does | Machine output | Exit codes |
|---|---|---|---|
| `status` | prints the build line, format version and command list | `--json` | `0` |
| `run` | `run -- cmd` runs a command under the recorder, watches for a stall, then recovers the tape | `--json` (one object on stdout after the child exits; the child's own stdout is untouched) | the child's exit code; `2` a command was not given or `blackbox.toml` was refused |
| `ingest` | the command a coding agent's hook runs (`--tool T --event E`): one inbox file from stdin | one line naming the inbox file it wrote **(stdout belongs to the agent; nothing else is printed)** | always `0` (fail-open: a broken hook must not break the agent) |
| `hook` | `hook install\|remove\|status` — manages the hook adapters | `--json` | `0` done, `2` usage or a refused settings file |
| `view` | `view <capsule\|dir> [--out]` — writes one offline HTML page | `--json` | `0`, `2` the target cannot be read |
| `serve` | `serve <capsule\|dir> [--port]` — serves the page on loopback for one fetch | `--json` (`url`, `bytes`, `one_shot`) | `0` served or stopped, `1` nothing fetched it before the timeout, `2` the target cannot be read |
| `desktop` | `desktop [dir]` — opens the app window (or `--selftest`) | `--json` | `0`, `2` usage, `3` no display or an incomplete window toolkit |
| `diagnose` | `diagnose <capsule\|dir> [--out] [--brief]` — read-only diagnosis, the Markdown report and the repair brief | `--json` | `0`, `1` nothing readable to diagnose, `2` a write was refused |
| `repair` | `repair preview\|apply\|verify` — the approval gate in front of a proposal someone else wrote | `--json` | `0` done, `1` an apply failed part-way and the rollback ran, `2` refused and **nothing written**, `3` verification failed or was not run |
| `link` | `link <a> <b>` — declares that two tapes describe one run, then prints the resolved graph | always JSON (the document is the output) | `0` recorded (`0` even when it resolved to nothing — the declaration *is* the answer), `2` a directory has no identifiable run |
| `capsule` | `capsule verify <capsule>` — recomputes the derived parts and reports drift | `--json` | `0`, `1` something drifted, `2` the capsule cannot be opened |
| `verify` | `verify <capsule…>\|--run\|--writer` — five checks against the capsule's own frames | `--json` | `0` every check passed, `9` a check failed, `2` a target cannot be read |
| `ls` | `ls [--data DIR]` — lists the capsules in a data directory | `--json` | `0` listed (an empty directory is an answer, not an error), `2` the data directory is a file |
| `index` | `index [--rebuild] [--dump]` — builds the data directory's index from its capsules | `--json` | `0` built (unreadable capsules are reported), `2` the data directory cannot be used |
| `retention` | `retention [--max-bytes] [--keep-hours]` — explains what retention would delete, and why; **never deletes** | `--json` | `0` a plan was produced, `2` the data directory or tape cannot be read |

## Why `retention` cannot delete

Deletion has to be *declared* first: a `GAP` frame says "this range is gone", and only the tape's writer can commit one.
A command that unlinked a segment without that frame would leave a tape that looks whole and is not, so `retention`
produces the plan, prints the span the real pass would declare, and leaves the deleting to the writer that owns the tape.

## Why `link` has no `--json`

It has nothing else: the resolved link document *is* JSON, on stdout, always. Adding a flag that changes nothing would be
a lie about the interface, so the column says `always JSON` and the test requires that its output parse.

## Why `status` grew one

`status` used to be a sentence for humans only. A script that wants to know "which build is installed, what does its
format expect" was reduced to parsing prose or asking `--version` and inferring the rest; the object now carries
`version`, `format_version`, `platform`, `python` and the command list.

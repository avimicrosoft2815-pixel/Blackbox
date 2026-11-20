# Troubleshooting — what you saw, what it means, what to do

Every entry here is a sentence the product actually prints, or a code that is in `docs/errors.md`, followed by the
smallest action that changes it. If the action does not change it, the last section says how to hand the problem to
somebody with the evidence attached; nothing here asks you to delete a recording.

## The app did not capture anything

**You see** `blackbox: this run recorded no model call — the 2 capsules of run r_… hold no request and no response.`
followed by a paragraph about base URLs.

**What it means** the command ran, the tape worked, and no model traffic reached the recorder. The usual cause is a
base-URL variable your shell, your `.env` or your code sets: the recorder adopts a base URL it finds rather than
overwriting it, so the client keeps talking to the address you configured.

**What to do**

```sh
blackbox run --force-proxy -- python app.py     # point every base-URL variable at the recorder, and it names each one
blackbox doctor --capture                       # which seam would install here, and why the others would not
```

`--force-proxy` prints the variables it displaced. If the run is still empty, the client is not reading them (a hard-coded
URL, a proxy-bypassing client, or a language runtime the recorder has no seam for — Node has a proxy route but no node
API, see `docs/compatibility.md`).

## The window does not open

**You see** `window backend: none (GTK/WebKit ready: False)` and a `missing:` line from `blackbox desktop --selftest`.

**What it means** the CLI is installed and the window's dependencies are not. Nothing is broken.

**What to do**

```sh
blackbox desktop --selftest                       # names exactly what is missing on this machine
pip install "blackbox-recorder[desktop]"          # the Python half (pywebview)
sudo apt install -y python3-gi gir1.2-webkit2-4.1 # the distribution half — see below if this fails
```

The package name is `gir1.2-webkit2-4.1` on Debian 12 and Ubuntu 22.04+; on Fedora it is `python3-gobject` plus
`webkit2gtk4.1`. Copying `pip`'s half alone is the most common mistake here: GTK is not a wheel, and no wheel substitutes
for it. Until both halves are present the CLI, `view`, `serve`, `diagnose` and every capsule command keep working — the
window is the only thing that needs them.

## `blackbox verify` exits 9

**You see** a table of checks with one `FAIL`, and a line like `BBX-E412 graph.json.nodes: …`.

**What it means** `verify` recomputes every derived part from the raw frames and compares. A `FAIL` is either a modified
file (E408), a damaged capsule (E402/E410/E412) or a defect — and the code says which. Exit codes: **0** all verified,
**9** a check failed, **2** a target could not be read.

**What to do** read the row that failed before doing anything else:

```sh
blackbox verify capsules/*.bbx          # every capsule, worst outcome decides the exit code
blackbox view capsules/c_….bbx --out page.html   # the failed check is a red banner in the page
```

E412 on a capsule sealed by a build before 2026-11-20 was a real defect (110 in `docs/defects.md`): a derived sentence
was built from the process clock and could not be recomputed later. It is fixed; a capsule recorded by an older build
keeps the failure and is still readable.

## `curl` stops with "Malformed encoding found in chunked-encoding"

Fixed on 2026-11-20 (defect 109 in `docs/defects.md`). If you see it on a build older than that, the proxy was writing a
`Transfer-Encoding: chunked` head over a body it relayed raw. Upgrade; the capture was not affected either side of the fix.

## A capsule's row says *needs attention*

**The words** `unreadable`, `cut-short tail`, `evidence lost from a sequence`, `declared gap`, `status not clean`,
`no model call`, `capture not full`. Each is a different fact and `docs/format.md` defines them.

**What to do**

```sh
blackbox diagnose capsules/c_….bbx --out incident.md    # the findings, their strength, and their alternatives
```

A `cut-short tail` is what a killed process leaves: the capsule is complete for everything it recorded and the last
sequence may be missing. A `declared gap` is a retention pass that removed a segment *and said so*; `verify` treats it as
a hole rather than a failure.

## The repair sheet will not apply

**You see** `BBX-E231` or "the approval token does not match".

**What it means** the token is bound to the proposal's bytes and to each target file's current hash. A file that changed
since the sheet was drawn invalidates it — deliberately, because the alternative is writing a diff into a file it was not
reviewed against.

**What to do** run `blackbox repair preview <proposal> --repo <dir>` again and read the new sheet. Nothing is written by
`preview`, and nothing is written by `apply` without `--approve <token>`. The commands a proposal suggests are only run
by `blackbox repair verify <proposal> --test '<command>'`, which is a separate decision.

## The recorder made my program slower, or changed its output

It should not, and the fail-open suite exists to keep it that way (`tests/test_failopen.py`): a recorder that cannot write
goes quiet and the app's return value, stdout and exceptions stay identical. If you see a difference:

```sh
BLACKBOX_DISABLED=1 python app.py         # the control: no recorder at all
BLACKBOX_HOME=/tmp/bbx-check python app.py # the same app, a fresh data directory
```

A difference that survives both is in the app. A difference that appears only with a data directory names the recorder,
and the file to attach to a report is `docs/format.md`'s "what a bug report needs" plus the capsule.

## Something is wrong with a tape or a data directory

```sh
blackbox ls                       # what is recorded, how big, and the status of each capsule
blackbox retention                # one row per segment: delete/keep/pinned/active, and why — deletes nothing
blackbox retention --json         # the same plan, machine-readable
blackbox doctor                   # permissions, seams, tapes, hooks, free space and leases, in one table
blackbox status --json            # the build line and the data directory, machine-readable
```

`retention` never deletes. A deletion needs `--gc` without `--dry-run`, and a referenced blob is never a candidate.

## Reporting a problem with evidence

Do not send a capsule to anybody without deciding that yourself: a capsule holds recorded prompts and completions under
your content policy. There is no telemetry, no crash upload and no update check in this product (`docs/privacy.md`).

What makes a report actionable, in this order:

1. `blackbox --version` and `blackbox desktop --selftest` output.
2. The exact command, and the exit code — every command documents its own (`docs/cli.md`).
3. The code (`BBX-E…`) and the one line around it.
4. If a capsule is involved: the output of `blackbox verify <capsule>` and, if you are willing to share it,
   `blackbox diagnose <capsule> --out incident.md`. Both are text; both name what they could not check.

What this repository will not do with a report: claim a cause it has not observed. `docs/defects.md` keeps the difference
between a defect that was reproduced, one that was fixed and verified, and one that is still open because the machine that
could settle it is not available — and defect 108 is the example of the last kind.

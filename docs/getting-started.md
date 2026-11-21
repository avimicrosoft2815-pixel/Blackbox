# Your first hour — the eight steps, in the order a beginner meets them

This page is one path, start to finish, for somebody who has installed nothing and knows nothing about this project: get
it running, point it at an app you already use, make a recording, open it, read what the recording supports, hand a brief
to a coding assistant, approve the change it proposes, and run the checks yourself.

Every step has three parts: **do this**, **you should see**, and **if it goes wrong**. The third part is the reason this
page is longer than a cheat sheet — the failure a beginner actually hits is rarely exotic, and a page that only shows the
happy path turns a five-minute problem into an evening.

Two rules hold for the whole page, and neither is a formality:

* **Nothing here sends your data anywhere.** No step needs an account, a key of ours, or a network route except the one
  your own app already uses to reach its provider. The diagnostics in step 6 are *text you copy*; the page cannot send
  them.
* **A step that has not been done by a person is marked unrun.** Where this repository has only a test, a CI run or a
  loopback origin to point at, it says so instead of describing what a window would look like.

Each step ends with the file or run that holds its evidence, so a reader can check the claim rather than trust this page.
`tests/test_getting_started.py` fails the suite if a step loses its evidence, if a command named here stops existing, if
an error code here is not in the register, or if a step is removed.

---

## Step 1 — Install it

**Do this.** On Debian or Ubuntu, the package is a single file:

```sh
sudo apt install ./blackbox_0.1.0.dev0_amd64.deb
blackbox --version
```

On Windows, run `BlackBox-Setup-0.1.0.dev0.exe` and follow the wizard. It installs for your user only, so it asks for no
administrator password. From a checkout, with Python 3.11 or newer:

```sh
python3 -m venv .venv && .venv/bin/pip install -e . pytest && .venv/bin/blackbox --version
```

**You should see** `blackbox 0.1.0.dev0 (format v2)` — the version and the capsule format it writes, which are two
different numbers and both matter. Then:

```sh
blackbox doctor
```

which answers four questions about this machine: whether it can run the window, what will be recorded and what will not,
which SDK it found (naming the exact command for the one you have), and where the recordings live.

**If it goes wrong.**

* `invalid command 'bdist_wheel'` from the `pip install -e .` line means the build needs `wheel` on an offline machine:
  `pip install wheel`, then `pip install -e . --no-build-isolation`.
* `cannot execute: required file not found` on the installed `.deb` usually means the `python3` the launcher names is not
  present — the package depends on `python3 (>= 3.11)`, and `apt` says so when it installs.
* On Windows, a SmartScreen panel that says the publisher is unknown is expected: **the installer is unsigned**, and
  signing is a separate decision this project has not taken. If you are not comfortable with that, build from source.
* If `blackbox --version` prints a different format number than this page, read `docs/format.md` before recording
  anything — a reader and a writer that disagree about the format is the one mismatch worth stopping for.

**Later — upgrading and removing it.** The package and your recordings are separate things, on both targets:

```sh
sudo apt install ./blackbox_<newer>.deb      # upgrade in place
sudo apt remove blackbox                     # removes the program, keeps the recordings
blackbox ls                                  # nothing recorded? then there was nothing to keep
```

Nothing above deletes a capsule: on Linux the `.deb` ships only files under `/usr` and has no removal script, and the
Windows uninstaller leaves `%LOCALAPPDATA%\BlackBox` in place — the installer says so during the uninstall rather than
letting you find out afterwards. Removing recordings is deliberate, with `blackbox retention --json` (which reports and
never deletes).

**Evidence.** The package is built and extracted on Linux in `tests/test_desktop_package.py`, which also asserts from the
built package that nothing installs outside `/usr`, that there is no `postrm`/`prerm`, and that `Version:` is orderable;
the installer is installed, run, installed a **second time over itself**, run again, uninstalled and the recordings counted
on a real Windows runner in `.github/workflows/package-windows.yml` (run `44868905784`, green, installer sha256
`a883f9a3…`). The wizard has **not** been used by a person on Windows 10 or 11 yet — see `docs/desktop.md`.

## Step 2 — Attach an app you already use

**Do this.** This is the whole attach, for anything that reads a base-URL environment variable:

```sh
blackbox run -- <your command>
```

For example `blackbox run -- python my_agent.py`, or `blackbox run -- node agent.mjs`. You do not edit your program: the
launcher exports `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` into the child, and a Python child additionally gets a
temporary `sitecustomize.py` on its `PYTHONPATH`, so the capture layer loads before your first line.

**Where it writes.** The data directory is **the directory you ran the command in**, so the recordings land in `capsules/`,
`runs/` and `tape/` beside your project. That is deliberate — an incident stays with the code that produced it — and it is
also the first thing that confuses a beginner, because the steps below use the same directory and a different terminal
window would not see it:

```sh
blackbox run --data-dir ~/.blackbox -- python my_agent.py  # put this run's recordings in ~/.blackbox instead
BLACKBOX_HOME=~/.blackbox blackbox run -- node agent.mjs  # the same choice, as an environment variable
```

`run` spells it `--data-dir`; `ls`, `verify`, `retention` and `doctor` spell it `--data`; `view` and `diagnose` take the
directory as their ordinary path argument (`blackbox diagnose ~/.blackbox`). Whichever you use, the read-only command has
to be pointed at the directory the recording went to rather than the one you happen to be standing in.

**You should see** one line naming the loopback port it started, then your program's own output, then a line naming the
capsule it sealed when the program exited or died. Nothing is printed about prompts or completions by default.

**Which apps are supported, honestly.** `docs/attach.md` keeps one row per surface with a version and a date, or the word
**unverified**. Four rows are verified — the Python `openai` and `anthropic` SDKs, `curl`, and Node's built-in `fetch` —
each on a loopback origin this repository built, not against a hosted provider. Sixteen rows are unverified, including
`openai` for Node and Go, Aider, LangChain, LiteLLM and Claude Code. "Unverified" means nobody has run it here; it does
not mean it fails, and it does not mean it works.

**If it goes wrong.**

* Your program printed its output but the capsule line says **no model calls recorded** — the app is not reading the
  variable. Most common cause: the client was constructed with an explicit `base_url=` (which wins), or the language's
  client uses a different variable name (`async-openai` and `ruby-openai` read `OPENAI_API_BASE`, which `blackbox run`
  does not export). `docs/attach.md` §2 says which is which.
* `BBX-E151` (from `blackbox hook install`) means a tool's settings file had a shape this build does not recognise. No
  file is written and no backup is made in that case — the refusal is the whole event.
* `BLACKBOX_DISABLED=1` anywhere in the environment makes capture a no-op on purpose. If a capsule is missing and nothing
  errored, check that first.

**Evidence.** `docs/attach.md`, whose rows `tests/test_attach_doc.py` refuses if a row has neither a version and date nor
the word "unverified"; the four verified rows are run by `tests/test_attach_sdks.py` through the launcher's own proxy, and
the killed-child recovery by `tests/test_attach_curl_kill.py`.

## Step 3 — Confirm the recording happened

**Do this.**

```sh
blackbox ls
blackbox verify capsules/*.bbx     # the directory you ran `blackbox run` in
```

`blackbox ls` lists what is in the data directory (the current one by default — see Step 2). `blackbox verify` recomputes the capsule's integrity index from the
bytes in the file and prints the root, the entry count, the size, and every problem it found.

**You should see** one row per run, newest first, with the run id, when it was recorded, the provider and model seen, and
a *needs attention* column. `verify` should print `ok` and a root hash; keeping that hash is how you know later that the
file has not changed.

**What "captured" does and does not mean.** The capsule records what the recorder could see: model calls through the
proxy, the node tree your program opened, stream chunks (including a stream the provider cut short), tool calls and
errors — and every gap the recorder knows about, declared as a gap rather than closed silently. It does not record other
programs' traffic, browser AI websites, or your source code. `docs/privacy.md` is the fuller answer and
`docs/desktop.md` prints the same two lists — recorded / not recorded — on the first screen.

**If it goes wrong.**

* `BBX-E402` "not a readable capsule archive" means the file is truncated or is not a `.bbx` at all. Check the path you
  passed; `verify` names the file it could not read.
* `BBX-E406` "encrypted or unknown-method capsule entry" means the capsule uses something this build does not implement.
  Do not try to open it with an older build — the refusal is the correct answer.
* `blackbox verify: error: name a capsule, or pass --run/--writer with --data` is not a corruption report: `verify`
  needs to be told which capsule. On Windows, prefer passing the path in from a shell rather than building it inside a
  script (defect 116 records a path that reached the CLI as `D`).

**Evidence.** `tests/test_verify_cli.py` and `tests/test_capsule.py`; the capsule attack corpus in
`tests/test_capsule_attacks.py` asserts hostile files are refused with a register code and no traceback.

## Step 4 — Open the incident

**Do this.** Either the window, or the terminal:

```sh
blackbox desktop
blackbox view capsules/            # or `blackbox view ~/.blackbox` if that is where you recorded
```

The window opens on the **library** when there is already something to show, and on **setup** when there is not — that
choice is made for you, from the manifests on disk. On Windows the Start-menu entry runs the same command.

**You should see** one row per capsule with the run, its recovery status, when it was recorded, the provider and model,
the event count, the size, and a *needs attention* reason where there is one: unreadable (with the code), a cut-short
tail, evidence lost from a sequence, a declared gap, a status that is not clean, no model call recorded, or a capture
that was not full. Click a row and the run opens with an action bar: *check this capsule*, *diagnose*, *repair a failed
run*.

The library reads **manifests only** — every header check the full reader makes, and no decompression — which is why a
hundred runs is a page rather than a wait. The trade is printed in the footer: a listed capsule is **unverified** until
you open it.

**If it goes wrong.**

* A row that says *needs attention: unreadable* is doing its job. Open it and the code is the same refusal `verify`
  gives (`BBX-E402`, `BBX-E406`), not an error page.
* `blackbox desktop` printing a message about GTK or WebKit and exiting 3 means this machine has no display or no
  `python3-gi`/`gir1.2-webkit2-4.1`. Use `blackbox view` in a browser, which serves the same pages.
* A window that opens on setup when you expected rows means the data directory it looked in is not the one you wrote
  to — the window reads the current directory by default, exactly as `blackbox run` writes it (Step 2). Start the desktop
  from the directory you recorded in, or pass `--data`.
  `blackbox doctor` names the directory it used; `--data` and `BLACKBOX_HOME` point it elsewhere.

**Evidence.** `tests/test_desktop.py` (window shell, fake backend), `tests/test_ui_library.py` (the library's columns,
search, sort and the manifest-only read), `tests/test_ui_safety.py` (the data directory is byte-identical after every
page load). The real window on a real display has **not** been run here — `docs/desktop.md` says so.

## Step 5 — Understand the findings

**Do this.** Press *diagnose* on the run, or:

```sh
blackbox diagnose capsules/              # a capsule, or a data directory (newest capsule wins)
```

**You should see** findings in four words, and the word is the point:

| word | what it means | how to treat it |
|---|---|---|
| `observed` | the capsule contains the bytes that prove this | act on it |
| `likely` | the evidence fits this, and the page says what else would fit | check the alternative before acting |
| `possible` | nothing rules it out | treat as a question, not a fact |
| `cannot-tell` | the capsule does not have what an answer needs | the page names which check would settle it |

Every finding carries its evidence, what else would fit, and what would settle it. The brief also says what it did
**not** record, for instance that a command line was not recorded rather than guessing one.

**If it goes wrong — read this part twice.** A diagnosis is the most seductive output this tool produces, so the
failures worth knowing are not crashes:

* A `likely` that you read as `observed` is the mistake this vocabulary exists to prevent.
* A brief that quotes a string that looks like a credential should not exist: every quoted value goes through the same
  pattern table the writer redacts with (`docs/reviews/redaction.md`). If you see one, that is a defect worth reporting, not
  something to paste into a chat window.
* `--max-events` caps how many frames are parsed (5 000 by default). A `cannot-tell` that appears only on a very large
  capsule may be the cap talking; the report says so where it applies.

**Evidence.** `tests/test_diagnose.py` asserts each word against a real capsule — a mid-stream interruption is `likely`
and carries its alternatives, a provider error is `observed`, and no finding reaches `observed` for something the capsule
only implies. The same four words are what the repair sheet in step 7 renders (`src/blackbox/diagnose/report.py`).

## Step 6 — Prepare a brief for a coding assistant

**Do this.**

```sh
blackbox diagnose capsules/ --brief repair-brief.md
```

or, in the window, *save the brief*.

**You should see** a markdown document you can read without any other file: what failed, what the capsule supports, what
it does not, the exact commands it cannot reproduce (named as unrecorded), the vocabulary from step 5 on every claim, and
— where it can — a machine-readable section a coding assistant can act on.

**The permission question is yours, and it is a real one.** Reading a brief aloud to another AI is sharing incident data,
and this project does not do it for you: nothing in the window makes a network call, and there is no button that sends a
brief anywhere. If your brief quotes a prompt or a completion from your content policy, **read it before you paste it**.
`docs/reviews/privacy.md` states what a brief can contain, and `docs/privacy.md` is the product-facing page.

**If it goes wrong.**

* `--brief` refuses to overwrite an existing file without `--force`. That is deliberate: a brief is evidence, and the
  second write of the same name would silently replace the first.
* An empty or near-empty brief on a capsule with a `D` status usually means the tail was cut short. Step 4's *needs
  attention* column says which; `docs/reviews/recovery.md` collects what the recoverer keeps and what it refuses to invent.

**Evidence.** `tests/test_diagnose.py` (the brief's sections, its unknowns, and the command line it refuses to guess),
`tests/test_redaction_every_sink.py` (a secret appears in no sink, the brief among them), and `tests/test_ui_flows.py`
(the *save the brief* route hands back the same bytes).

## Step 7 — Review and approve the proposal

**Do this.** In the window, *repair a failed run*, then choose the proposal file and the repository it applies to. If you
have no proposal, the brief from step 6 is what you give to a coding assistant to produce one.

**You should see** the whole sheet before any decision: the scope, every file as a diff against what is on disk **now**,
the risk notes, and the tests the proposal suggests — marked *not run by this page*. Under it, two separate buttons:

* **Apply exactly this** — needs a ticked box *and* the first eight characters of an approval token the page shows. The
  token is bound to the proposal's bytes and to each target file's current hash, so if anything moved since the sheet was
  drawn the typed characters stop matching and the apply refuses, naming what changed.
* **Run the tests** — a different button, because it *executes commands*. The boxes start from the tests the proposal
  suggests, you can type your own, and it runs **what you tick**, in your repository, naming each command before it runs
  and reporting each exit code and the tail of its output. Nothing from the capsule is ever executed, and an unticked box
  runs nothing.

On success the page says where the verified backup went (`.blackbox-repair/backup-…` inside your repository) and which
files it wrote. Neither button can be triggered by a link: both are `POST`s that must carry this process's token and come
from this app's own origin.

**If it goes wrong.**

* "The token does not match" after a tick and a correct-looking token means a file changed while the sheet was open —
  which is the refusal working. Re-draw the sheet.
* An apply that reports *no backup path* should not happen; if it does, stop and read `docs/troubleshooting.md` before
  running anything else in that repository.
* An empty command list is reported as *unverified*, not as a pass. That sentence is the difference between this step and
  a green tick somebody imagines.

**Evidence.** `tests/test_repair_workflow.py` (the token bound to the proposal's bytes and each target's hash, the
refusal when a file moved, the verified backup path) and `tests/test_ui_flows.py` (both routes, both refusals — no tick,
no token — and `executes: true` on the result so a caller cannot mistake it for a dry run).

## Step 8 — Run the authorized checks, and read the result honestly

**Do this.** Press **Run the tests**, or from the terminal:

```sh
blackbox repair preview proposal.json --repo .
blackbox repair verify proposal.json --repo . --test "pytest -q"
```

**You should see** one block per command: the command, its exit code, and the tail of its output. Running them is not the
same as passing them, and an empty list is *unverified*, not *clean*.

One thing to be clear about, because the naming invites the opposite assumption: `repair verify` runs **the commands you
name with `--test`**, not the proposal's. The proposal's own `tests` list is displayed by `preview` and never executed —
a proposal is a file somebody or something else wrote, and this project does not hand a stranger's command list to your
shell. `preview` and `apply` execute nothing at all.

**If it goes wrong.** A command that fails is information, not a broken tool — read its own output. A command the
proposal suggested but you did not intend to run is a reason to read the sheet again before pressing anything: this page
runs what the proposal says, not what you wish it said.

**Evidence.** `tests/test_repair_workflow.py` (the command runner: the commands it ran, in order, before running them)
and `tests/test_ui_flows.py` (`run_tests` refuses without a tick, refuses with no command, and says `executes: true`);
`bench/results/2026-11-20-desktop-journey-automated.json` is the automated journey's own record.

---

## What this page is not

* **Not a claim that a person has done all eight steps on Windows.** Steps 1 and 3 ran unattended on a real Windows
  runner (`.github/workflows/package-windows.yml`, run `44807593306`); the wizard, the Start-menu entry and the window
  have not. `docs/desktop.md` has the matrix.
* **Not a claim that a person has done the window journey on Linux either.** The machine that wrote this has no display;
  the pages are exercised over loopback by `tests/test_ui_*.py` and by
  `sh tools/desktop_journey.sh --automated-only`, which records a real incident, lists it, verifies it and diagnoses it,
  and writes `"verdict": "automated-only"` with every window answer `"not-attempted"`.
* **Not a promise about any provider.** Sixteen attach rows are unverified, and no hosted provider has been contacted from
  this repository.
* **Not a support commitment.** There is no account, no telemetry and no update check; if a step breaks, the error code
  and `docs/troubleshooting.md` are the support.

## Where each step's machinery lives

| Step | Code | Page |
|---|---|---|
| 1 Install | `tools/package/linux/build_deb.sh`, `tools/package/windows/` | `docs/desktop.md`, `docs/cli.md` |
| 2 Attach | `rec/launcher.py`, `proxy/supervisor.py` | `docs/attach.md` |
| 3 Confirm | `cap/format.py`, `cli/cmd_verify.py` | `docs/format.md`, `docs/errors.md` |
| 4 Open | `ui/app.py`, `ui/desktop.py`, `cap/derived.py` | `docs/desktop.md` |
| 5 Findings | `diagnose/findings.py`, `diagnose/evidence.py` | `docs/guarantees.md` |
| 6 Brief | `diagnose/report.py`, `redact.py` | `docs/privacy.md`, `docs/reviews/redaction.md` |
| 7 Approve | `repair/proposal.py`, `repair/apply.py`, `ui/flows.py` | `docs/troubleshooting.md` |
| 8 Checks | `repair/apply.py` (`verify`), `repair/cli.py` | `docs/limitations.md` |

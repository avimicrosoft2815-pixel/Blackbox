# The BlackBox desktop app

BlackBox ships as a **desktop application** — a real window with a title bar, a menu entry and a launcher icon — not as a
terminal command and not as a browser tab. This page says how to install it, what it does when you open it, what it
deliberately does not do, and exactly which of those claims have been run on a real machine.

## Install

### Debian / Ubuntu (Linux) — the packaged path

```sh
sudo apt install ./blackbox_0.1.0.dev0_amd64.deb
blackbox desktop            # the window
blackbox --version          # the build line
```

The package installs `/usr/bin/blackbox`, its library under `/usr/lib/blackbox/`, a `.desktop` entry, a scalable icon,
and a MIME type so a double-clicked `.bbx` capsule opens in the viewer. It declares `Depends: python3 (>= 3.11),
python3-gi, gir1.2-webkit2-4.1`: the window embeds the **system** webview (WebKitGTK), so there is no bundled browser
engine, no downloaded runtime and no account.

Build it yourself, from a checkout, with network available once (for the `pywebview` wheel):

```sh
tools/package/linux/build_deb.sh --out dist
sudo apt install ./dist/blackbox_0.1.0.dev0_amd64.deb
```

### Any OS, from PyPI — the developer path

```sh
pip install "blackbox-recorder[desktop]"       # the window; the core stays stdlib-only
blackbox desktop
```

On Debian/Ubuntu the wheel is not enough for the window — GTK is a distribution package:

```sh
sudo apt install -y python3-gi gir1.2-webkit2-4.1
```

`blackbox desktop --selftest` prints exactly what is missing on the machine you are on; it is the first thing to run when
the window does not appear.

### Windows

`tools/package/windows/blackbox.iss` is the installer definition; `tools/package/windows/README.md` carries the exact
build and hand-off steps. **No Windows installer has been built or run in this build environment**, and the Windows
desktop journey is therefore *unverified* here — see the matrix below.

Re-checked on 2026-11-19 before writing this line: `uname -sm` is `Linux x86_64`, and `command -v makensis iscc wine
wine64 winecfg pwsh powershell` finds **none of them**. There is no Windows host, no Inno Setup, no Wine and no
PowerShell, so the unsigned installer cannot be built or exercised from here — the attempt is recorded instead of
inferred, and the hand-off in `tools/package/windows/README.md` is what a Windows machine needs. Signing is the owner's
separate task: nothing in this repository is signed.

What *is* checked here, by `tests/test_windows_definition.py` (11 tests): every file the definition names exists (which is
how the missing `blackbox.ico` was found — defect 90), the icon is a real ICO with the product's colours at 16/32/48/256,
the installer is per-user, writes only under `HKCU`, deletes only its own directory on uninstall, claims no signature it
does not have, and the staging script and the definition agree on the launcher. That is a definition that would compile.
It is not an installer that has been run, and the matrix row below says so.

### macOS

**Experimental and untested.** `pywebview` drives WKWebView there and the code path is the same, but nothing in this
repository has been run on a Mac. Do not read the missing warning as support.

## What opening it gives you

The window opens on one of three pages, and which one is a decision the app makes for you:

**1. Setup, on a machine with nothing recorded.** The first screen is not an empty table — it is four answers: whether
this machine can run the window at all, what will be recorded and *what will not*, which SDK is installed (with the exact
command for the one you have), and where the recordings live. Each check shows the value it observed (`python 3.11.9`,
`0.4 GB free`, `OPENAI_API_KEY is set — its value is never read`) and every check that is not fine carries a command or a
sentence naming what to install. Three states: *blocked*, *ready*, *complete*.

The two lists are the honest core. **Recorded:** model calls the recorder can see, the node tree your code opens, stream
chunks including a stream the provider cut short, tool calls and errors, and every gap the recorder knows about.
**Not recorded:** nothing leaves the machine (no account, cloud, telemetry or update check); prompts and completions only
under your content policy, with secrets redacted by default; other programs' traffic; browser AI websites (this build
records SDK and HTTP clients, not an extension); and your source code.

**2. The library, once there is something to open.** One row per capsule, newest first — the run, its recovery status,
when it was recorded, which provider and model were seen, how many events, how big, and a *needs attention* column that
names the reason: unreadable (with the refusal code), a cut-short tail, evidence lost from a sequence, a declared gap, a
status that is not clean, no model call recorded, or a capture that was not full. Search across run, capsule, provider,
model and status; filter by status; sort by newest, oldest, largest, status or run; 25 rows a page.

The library reads **manifests only** — `cap.format.read_manifest`, which makes every header check the full reader makes
(same refusal codes, proven in `tests/test_cap_read_manifest.py`) and decompresses no entry body. That is what makes a
directory of a hundred runs a page rather than a wait. The trade is stated in the footer and in the row: a capsule listed
here is **unverified** until you open it, and opening it recomputes the integrity index exactly as `blackbox verify` does.

**3. A run.** The existing offline viewer — black-and-gold glass, keyboard-first (roving tabindex, skip link, `?` for the
shortcut dialog, `prefers-reduced-motion` honoured), every capsule value inserted as text so recorded text cannot become
markup, a failed integrity root shown as a red banner rather than a timeline — with an **action bar**: back to the
library, *check this capsule*, *diagnose*, *repair a failed run*.

**4. The check.** `Check this capsule` recomputes the capsule's integrity index from the bytes in the file — the same work
`blackbox verify` does — and prints the root, the entry count, the size, the time, and every problem it found. A capsule
that cannot be read is reported with its refusal code (`BBX-E402`, `BBX-E406`) rather than an error page.

**5. The diagnosis, and the repair gate.** `Diagnose` renders the findings in the brief's own vocabulary — `observed`,
`likely`, `possible`, `cannot-tell`, each with its evidence, what else would fit, and what would settle it — then the
repair brief itself as **text you can copy**. The app never sends it anywhere: there is no network call in the page and no
button that makes one.

`save the brief` and `save the report` hand you the two documents as files. The app itself still writes nothing: your
browser saves the file you asked for, at your click.

`Repair a failed run` takes a proposal file and a repository and shows the whole sheet: scope, every file as a diff against
what is on disk now, the risk notes, and the tests the proposal suggests (marked *not run by this page*). Under it, two
separate decisions:

* **Apply exactly this** — requires a ticked box *and* the first eight characters of an approval token that the page
  shows. The token is bound to the proposal's bytes and to each target file's current hash, so if anything moved since the
  sheet was drawn the typed characters no longer match and the apply refuses, naming what changed. On success the page
  says where the verified backup went (`.blackbox-repair/backup-…` inside your repository) and which files it wrote.
* **Run the tests** — a different button, because it *executes commands*. It runs only the commands the proposal
  suggested (shown before the button), in your repository, and reports each exit code and the tail of its output. Running
  them is not the same as passing them: an empty command list reports the change as unverified.

Neither action can be triggered by a link: both are `POST`s that must carry this process's token and come from this app's
own origin, and the result is fetched by an unguessable id so a refresh cannot repeat it.

**What the window adds over the file:** nothing is written to disk, no browser profile is touched, the page cannot
navigate anywhere (three routes, all served by this process under one secret path), and closing the window stops the
server. Pages are server-rendered: the library and setup pages need no JavaScript at all, and the only script is the
viewer's own keyboard tree on a run page.

## How it works, in one paragraph

The app reads the manifests under the data directory to decide whether to open setup or the library, starts a loopback
server — bound to `127.0.0.1` on a port the kernel chooses, serving **ten `GET` routes** under one secret path that is
generated per process and never logged — points a window at the front page, and stops the server when the window closes.
Every page is a `GET`, and so are the two export downloads (the brief, the report); the only two writes are `POST`s
(apply a reviewed proposal, run the commands the person chose), each requiring the ticked box and, for an apply, the
typed approval token. A `GET` with any URL at all cannot write, record or delete anything — `tests/test_ui_safety.py`
hashes the data directory before and after every page load. A capsule id in a URL is *matched* against the manifests,
never joined onto a path, so `../` is not a request the server can serve. Nothing listens on a network interface,
nothing is reachable from another machine, and there is no daemon left behind. `tests/test_desktop.py` and
`tests/test_ui_library.py` assert the loopback bind, the secret path, `debug=False` on the window, the same refusals
from both capsule readers, that no secret appears in the setup page, that the page references no external URL, and that
the listening socket is closed afterwards.

## Privacy, stated plainly

* **No account, no cloud, no telemetry.** The app makes no network request of its own. `tests/test_no_egress.py` and the
  netguard rows cover the recorder as a whole.
* **Local storage only**, under the data directory (`BLACKBOX_HOME`, else the platform default). `blackbox ls` shows what
  is there; retention and deletion are documented in `docs/guarantees.md` and `docs/STATUS.md`'s retention rows.
* **Capture is what the policy says.** The app shows whether capture is active, from which vantage (SDK, proxy, hook) and
  which provider was recognised; an unrecognised address is shown as unknown rather than guessed at.
* **Incident content is untrusted.** Recorded text is displayed, never executed; the repair brief is text you hand to
  your own assistant, and nothing is sent anywhere by the app.

## Platform matrix — what has actually been run

| Target | CLI + viewer page | Desktop window | Installer | Notes |
|---|---|---|---|---|
| **Debian 12 (bookworm), x86_64** | **run here**: the full suite, `blackbox view/serve`, `desktop --selftest`, the library and setup pages fetched over loopback | **not launched here** — the build machine has no display, no GTK and no WebKit | **`.deb` built and extracted here**; `usr/bin/blackbox --version` runs from the extracted tree | The window path is covered by `tests/test_desktop.py` with a fake webview; the real window needs a machine with `python3-gi` + `gir1.2-webkit2-4.1` |
| **Ubuntu 24.04 LTS, x86_64** | same code, CI's `ubuntu-latest` cells | **not launched** | same `.deb` recipe (`gir1.2-webkit2-4.1` is the package name there too) | `ubuntu-latest` CI runs the CLI, not a GUI |
| **Windows 10/11, x64** | **CI only**: `windows-latest` cells run the suite (3/3 at `1ec8be9`, the CLI — not the window) | **not run** — no Windows machine, no `makensis`/`iscc`, no `wine`, no PowerShell here (re-checked 2026-11-19) | installer definition written, **not built**, **unsigned** | Read as *unverified*: no Windows desktop journey has happened |
| **macOS** | CI `macos-latest` cells when a runner is granted (cancelled by capacity at the last two commits) | **not run** | none | **Experimental / unsupported** |

The distinction that matters: *"the code path is tested on Linux"* is not *"the app has been used on Windows"*. The
second column is evidence, the third is a test double, and the last two rows are honest blanks.

## Upgrade, uninstall, and your data

```sh
sudo apt install ./blackbox_<newer>.deb     # upgrade in place
sudo apt remove blackbox                    # removes the program
```

**Your recordings are not the package's.** Capsules and tapes live in your data directory, and neither `apt remove` nor
an uninstaller deletes them: removing the app leaves the evidence where it was, which is the correct default for a
recorder and is stated in the installer rather than discovered later. Delete them deliberately, with the retention tool:

```sh
blackbox ls                      # what is recorded, and how big
blackbox gc --dry-run            # what a cleanup would remove, and why
```

## Turning "tested" into "seen" — the first launch

Everything below the window has an automated test; the window itself needs a machine with a display. On the machine you
intend to support, run the journey:

```sh
sh tools/desktop_journey.sh                        # the whole thing, and it asks you six questions
sh tools/desktop_journey.sh --app 'python3 my_service.py'   # record your own app instead of the sample
sh tools/desktop_journey.sh --automated-only       # no screen: records, lists, checks, diagnoses, writes the file
```

It checks the machine (distribution, display, window stack), records an incident — a four-line program that raises on
purpose, unless you name your own — lists it, opens the window and walks you through setup → library → run → check →
diagnose → close, then checks the same capsule from the command line and writes `desktop-journey-<time>.json`:

```json
{"schema": "blackbox.desktop-journey/1", "verdict": "pass | incomplete | automated-only",
 "automated": {"run_exit": 1, "window_exit": 0, "verify_exit": "0", "diagnose_exit": "0"},
 "seen_by_a_person": {"window_opened": "yes", "run_opened": "yes", "check_verified": "yes", ...}}
```

That file is the evidence for one row of the matrix above, on one distribution and architecture, and for nothing else.
A "no" anywhere keeps the row at *not run* — including the case where the window opened but the run did not.

`tools/desktop_smoke.sh` is the shorter first-launch check (CLI, window stack, open, close) for a quick sanity run; both
stop at the first thing they cannot decide for themselves, so a machine with no display gets one clear sentence.

**On this build machine**, the journey has been run with `--automated-only`: it records, lists, checks and diagnoses
(exit 1 / 0 / 0, all green), and writes `"verdict": "automated-only"` with every window answer `"not-attempted"` —
because there is no display, no GTK and no WebKit here. `tests/test_desktop_journey.py` asserts exactly that, including
that the harness refuses on a headless machine instead of writing a pass.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no window can be opened here: missing pywebview` | the optional extra is not installed | `pip install "blackbox-recorder[desktop]"`, or use the `.deb` |
| `missing python3-gi + gir1.2-webkit2-4.1` | the system webview is not installed | `sudo apt install -y python3-gi gir1.2-webkit2-4.1` |
| The window opens blank | a WebKit typelib version mismatch | `blackbox desktop --selftest`; it reports which GTK/WebKit versions it found |
| Nothing happens on a server / over SSH | there is no display | this is a desktop app; use `blackbox serve <path>` and open the URL it prints |
| Double-clicking a `.bbx` opens an archive manager | the MIME database has not been rebuilt | `sudo update-mime-database /usr/share/mime` |

## What is not finished

* **Windows**: the installer definition is written and checked (see above) but has never been compiled, and the journey
  has never been run. Building it needs Inno Setup 6 on a Windows machine; the journey needs a person on Windows to
  install, launch, record, open, diagnose and uninstall. That is the release gate's remaining human/machine requirement
  for Windows, and it is listed as *unverified*, not *passed*.
* **Signing**: neither the `.deb` nor a Windows installer is signed. That needs the owner's certificate, which is not in
  this environment.
* **A real window on Linux**: the build machine cannot open one, so the first launch on a desktop Linux host is an
  unperformed step. `blackbox desktop --selftest` turns that into a five-second check.
* **A library over thousands of capsules** is paginated and manifest-only, but it is verified on tens of capsules here,
  not thousands: `tests/test_ui_library.py` scans 20, and no measured figure for 5 000 runs exists yet.

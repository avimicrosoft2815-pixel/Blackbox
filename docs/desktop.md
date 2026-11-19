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
markup, a failed integrity root shown as a red banner rather than a timeline — with a bar back to the library.

**What the window adds over the file:** nothing is written to disk, no browser profile is touched, the page cannot
navigate anywhere (three routes, all served by this process under one secret path), and closing the window stops the
server. Pages are server-rendered: the library and setup pages need no JavaScript at all, and the only script is the
viewer's own keyboard tree on a run page.

## How it works, in one paragraph

The app reads the manifests under the data directory to decide whether to open setup or the library, starts a loopback
server — bound to `127.0.0.1` on a port the kernel chooses, serving **three routes** under one secret path that is
generated per process and never logged — points a window at the front page, and stops the server when the window closes.
Every route is a `GET`: there is nothing in the app that writes, records or deletes, so a guessed token can read what the
token protects and cannot change anything. A capsule id in a URL is *matched* against the manifests, never joined onto a
path, so `../` is not a request the server can serve. Nothing listens on a network interface, nothing is reachable from
another machine, and there is no daemon left behind. `tests/test_desktop.py` and `tests/test_ui_library.py` assert the
loopback bind, the secret path, `debug=False` on the window, the same refusals from both capsule readers, that no secret
appears in the setup page, that the page references no external URL, and that the listening socket is closed afterwards.

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
intend to support:

```sh
sh tools/desktop_smoke.sh                 # checks the CLI, the window stack, opens a window, closes it
sh tools/desktop_smoke.sh capsule.bbx     # or point it at one capsule
```

It prints one line per check and stops at the first failure, so the output can go straight into the platform matrix
above. Over SSH or in a container it will stop at check 2 and say what is missing, which is the correct answer for a
desktop app.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no window can be opened here: missing pywebview` | the optional extra is not installed | `pip install "blackbox-recorder[desktop]"`, or use the `.deb` |
| `missing python3-gi + gir1.2-webkit2-4.1` | the system webview is not installed | `sudo apt install -y python3-gi gir1.2-webkit2-4.1` |
| The window opens blank | a WebKit typelib version mismatch | `blackbox desktop --selftest`; it reports which GTK/WebKit versions it found |
| Nothing happens on a server / over SSH | there is no display | this is a desktop app; use `blackbox serve <path>` and open the URL it prints |
| Double-clicking a `.bbx` opens an archive manager | the MIME database has not been rebuilt | `sudo update-mime-database /usr/share/mime` |

## What is not finished

* **Windows**: the installer definition exists; building it needs `makensis` (Inno Setup) on a Windows machine, and the
  journey needs a person on Windows to install, launch, record, open and uninstall. That is the release gate's remaining
  human/machine requirement for Windows, and it is listed as *unverified*, not *passed*.
* **Signing**: neither the `.deb` nor a Windows installer is signed. That needs the owner's certificate, which is not in
  this environment.
* **A real window on Linux**: the build machine cannot open one, so the first launch on a desktop Linux host is an
  unperformed step. `blackbox desktop --selftest` turns that into a five-second check.
* **A library over thousands of capsules** is paginated and manifest-only, but it is verified on tens of capsules here,
  not thousands: `tests/test_ui_library.py` scans 20, and no measured figure for 5 000 runs exists yet.

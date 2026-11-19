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

### macOS

**Experimental and untested.** `pywebview` drives WKWebView there and the code path is the same, but nothing in this
repository has been run on a Mac. Do not read the missing warning as support.

## What opening it gives you

`blackbox desktop [path]`:

* with no argument, opens the standard data directory — the run list;
* with a `.bbx` capsule, opens that capsule;
* with a data directory, opens every capsule in it as a run rail with the linked tapes joined by `exact` links.

The page is the same offline viewer `blackbox view` writes: black-and-gold glass, keyboard-first (roving tabindex, skip
link, `?` for the shortcut dialog, `prefers-reduced-motion` honoured), every capsule value inserted with `textContent`
so recorded text cannot become markup, and a failed integrity root shown as a red banner rather than a timeline.

**What the window adds over the file:** nothing is written to disk, no browser profile is touched, the page cannot
navigate anywhere (there is one URL and it is served by this process), and closing the window stops the server.

## How it works, in one paragraph

The app builds the page in memory, starts the same loopback server `blackbox serve` uses — bound to `127.0.0.1` on a
port the kernel chooses, serving one secret path that is generated per process and never logged — points a window at
that URL, and stops the server when the window closes. Nothing listens on a network interface, nothing is reachable from
another machine, and there is no daemon left behind. `tests/test_desktop.py` asserts the loopback URL, the secret path,
`debug=False` on the window, and that the listening socket is closed afterwards.

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
| **Debian 12 (bookworm), x86_64** | **run here**: 1307-test suite, `blackbox view/serve`, `desktop --selftest` | **not launched here** — the build machine has no display, no GTK and no WebKit | **`.deb` built and extracted here**; `usr/bin/blackbox --version` runs from the extracted tree | The window path is covered by `tests/test_desktop.py` with a fake webview; the real window needs a machine with `python3-gi` + `gir1.2-webkit2-4.1` |
| **Ubuntu 24.04 LTS, x86_64** | same code, CI's `ubuntu-latest` cells | **not launched** | same `.deb` recipe (`gir1.2-webkit2-4.1` is the package name there too) | `ubuntu-latest` CI runs the CLI, not a GUI |
| **Windows 10/11, x64** | **CI only**: `windows-latest` cells run the suite (3/3 at `0427242`) | **not run** — no Windows machine or GUI CI in this environment | installer definition written, **not built** | Read as *unverified*: no Windows desktop journey has happened |
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
* **A library view over thousands of capsules** is a page that loads every capsule it is given; the viewer is verified
  on tens, not thousands, and no lazy loading exists yet.

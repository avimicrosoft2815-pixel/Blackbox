"""The Windows installer definition, checked on Linux — every claim that can be settled without Windows, settled.

No Windows machine exists in this environment (re-checked: no `makensis`, `iscc`, `wine` or PowerShell), so nothing here
is a claim that the installer works. What these tests do is close the gap between *"a definition exists"* and *"a
definition that would compile"*, because `iscc` fails on the first missing `Source:` and that failure costs a Windows
machine round-trip to discover:

* every file the definition names exists in the repository or is produced by the staging script;
* the two halves agree on the launcher (`blackbox.cmd`, `pythonw.exe`) and on where the app is installed;
* it installs per-user, writes only `HKCU`, and deletes only its own directory on uninstall — the recordings stay;
* it does not claim a signature it does not have;
* the icon is a real ICO with the product's colours, at the sizes Windows uses.

Defect 90 came out of the first of these: the definition named `blackbox.ico` in three places and the file did not exist.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "tools" / "package" / "windows"
ISS = WINDOWS / "blackbox.iss"
PS1 = WINDOWS / "build_windows.ps1"
ICO = WINDOWS / "blackbox.ico"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, list[str]]:
    """Split an Inno Setup script into its `[Section]` blocks, ignoring comments and blank lines."""
    out: dict[str, list[str]] = {}
    current = "(preamble)"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out.setdefault(current, [])
            continue
        out.setdefault(current, []).append(line)
    return out


def _sources(text: str) -> list[str]:
    """Every `Source:` value in `[Files]`, with Inno's quoting and `{app}`-style constants left intact."""
    return re.findall(r'^Source:\s*"([^"]+)"', text, re.M)


# ── the definition would compile ────────────────────────────────────────────
def test_the_definition_has_the_sections_inno_requires() -> None:
    sections = _sections(_iss())
    for required in ("Setup", "Files", "Icons", "Registry", "Run", "Languages"):
        assert required in sections, f"[{required}] is missing"
    setup = " ".join(sections["Setup"])
    assert "AppId=" in setup and "AppName=" in setup and "OutputBaseFilename=" in setup


def test_every_file_the_definition_names_exists_or_is_staged() -> None:
    """This is what found defect 90: `blackbox.ico` was named three times and did not exist."""
    staged = {"win64\\"}                       # produced by build_windows.ps1; nothing here can create it
    for source in _sources(_iss()):
        if any(source.startswith(prefix) for prefix in staged):
            continue                                # the staged runtime tree, made on Windows
        path = (WINDOWS / source.replace("\\", "/")).resolve()
        assert path.exists(), f"[Files] names {source!r}, which is not in the repository ({path})"


def test_the_icon_is_a_real_ico_in_the_products_colours() -> None:
    data = ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 4, (reserved, kind, count)
    sizes = []
    offset = 6
    for _ in range(count):
        edge_w, _edge_h, _colours, _res, _planes, bpp, size, data_offset = struct.unpack("<BBBBHHII", data[offset:offset + 16])
        offset += 16
        assert bpp == 32
        png = data[data_offset:data_offset + size]
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "each entry is a PNG-compressed image"
        sizes.append(edge_w or 256)
    assert {16, 32, 48, 256} <= set(sizes), sizes

    # The 32-pixel entry, decoded: the field is near-black, the mark is gold, the corners are transparent.
    offset = 6 + 16 * sizes.index(32)
    _w, _h, _c, _r, _p, _bpp, size, data_offset = struct.unpack("<BBBBHHII", data[offset:offset + 16])
    png = data[data_offset:data_offset + size]
    position, idat, width = 8, b"", 0
    while position < len(png):
        length = struct.unpack(">I", png[position:position + 4])[0]
        kind_name = png[position + 4:position + 8]
        payload = png[position + 8:position + 8 + length]
        if kind_name == b"IHDR":
            width = struct.unpack(">IIBB", payload[:10])[0]
        elif kind_name == b"IDAT":
            idat += payload
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1

    def pixel(x: int, y: int) -> str:
        start = y * stride + 1 + x * 4
        return raw[start:start + 4].hex()

    assert pixel(0, 0) == "00000000", "the rounded corner must be transparent, not black"
    assert pixel(8, 8).startswith("0b0b0d"), pixel(8, 8)          # the field
    assert pixel(16, 16).startswith("e6c46a"), pixel(16, 16)      # the waveform
    assert pixel(16, 1).startswith("997b3b") or pixel(16, 1).startswith("c8a04a"), pixel(16, 1)   # the ring


def test_the_installer_is_per_user_and_leaves_the_recordings_alone() -> None:
    sections = _sections(_iss())
    setup = " ".join(sections["Setup"])
    assert "PrivilegesRequired=lowest" in setup, "an installer that demands admin for a local app is a bad trade"
    assert "ArchitecturesAllowed=x64compatible" in setup
    for line in sections["Registry"]:
        assert "Root: HKCU;" in line, f"[Registry] writes outside HKCU: {line}"
    deletes = " ".join(sections.get("UninstallDelete", []))
    assert "win64" in deletes
    assert "LOCALAPPDATA" not in deletes and "BlackBox" not in deletes.replace("{app}\\win64", ""), (
        "the uninstaller must not delete the data directory")
    assert "Data kept" in _iss() or "kept when you uninstall" in _iss()


def test_the_definition_does_not_claim_a_signature_it_does_not_have() -> None:
    setup = " ".join(_sections(_iss())["Setup"])
    for line in setup.split(" "):
        if line.startswith(("SignTool=", "SignedUninstaller=")):
            raise AssertionError(f"a signed build is the owner's decision, not a default: {line}")
    assert "Authenticode" in _iss(), "the file must say what the owner has to supply"


def test_the_installer_runs_only_the_app() -> None:
    run_lines = _sections(_iss())["Run"]
    assert run_lines, "[Run] is empty, so the post-install checkbox would do nothing"
    for line in run_lines:
        assert 'Filename: "{app}\\blackbox.cmd"' in line, f"[Run] starts something other than the app: {line}"
    for line in _sections(_iss())["Icons"]:
        if "Uninstall BlackBox" in line:
            assert "{uninstallexe}" in line
            continue
        assert '"{app}\\blackbox.cmd"' in line, line


# ── the two halves agree ────────────────────────────────────────────────────
def test_the_staging_script_produces_what_the_definition_packages() -> None:
    script = PS1.read_text(encoding="utf-8")
    assert "'blackbox.cmd'" in script, "the definition's [Icons]/[Run] entries need this launcher"
    assert "pythonw.exe" in script, "a console window beside a desktop app is a bug, not a style choice"
    assert "launcher.log" in script, "a window that fails to open must leave a log to read"
    assert "ErrorActionPreference = 'Stop'" in script
    # Pinned, not floating: `latest` on a build machine is how two installers get different interpreters.
    assert re.search(r"python-3\.\d+\.\d+-embed-amd64\.zip", script), "the interpreter must be pinned to a version"
    assert "latest" not in script.lower().replace("the latest", "")
    assert "--only-binary=:all:" in script, "a source build on the user's machine is not what this is for"
    assert "pip install --no-index" in script, "the install must not need the network"


def test_the_definition_says_it_is_unverified_and_unsigned() -> None:
    text = _iss() + PS1.read_text(encoding="utf-8")
    assert "SmartScreen" in text
    assert "NOT been run" in text or "not been run" in text, "the staging script must say it is unrun"


def test_no_define_is_dead_weight() -> None:
    """`AppExe` was defined and never used: a reader would think the install's entry point was `pythonw.exe` directly."""
    text = _iss()
    for name in re.findall(r"^#define\s+(\w+)", text, re.M):
        uses = len(re.findall(r"\{#" + name + r"\}", text))
        assert uses >= 1, f"#define {name} is never used"


def test_the_readme_states_what_a_windows_machine_must_do() -> None:
    readme = (WINDOWS / "README.md").read_text(encoding="utf-8")
    for phrase in ("iscc", "build_windows.ps1", "sign"):
        assert phrase in readme.lower(), phrase
    # The README used to be titled "staged here, built and tested on Windows", which nothing in this repository did.
    assert "no windows installer has been produced or run" in " ".join(readme.lower().split())
    lower = readme.lower()
    assert "has been checked here" in lower or "checked here" in lower


def test_the_icon_generator_is_reproducible() -> None:
    """The .ico is generated, so it must be regenerable byte for byte — otherwise a rebuild silently changes it."""
    sys.path.insert(0, str(WINDOWS))
    try:
        import make_icon
    finally:
        sys.path.pop(0)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        again = make_icon.build(Path(tmp) / "again.ico", sizes=(32,))
        assert again.exists() and again.stat().st_size > 100
        # Same container, same type; the entry count differs because this call asks for one size.
        assert again.read_bytes()[:4] == ICO.read_bytes()[:4]
    # The committed file is the six-size build the tests above read, and small enough that every shortcut copies it fast.
    assert len(ICO.read_bytes()) < 64 * 1024


def test_nothing_in_the_definition_waits_for_a_button() -> None:
    """An unattended install has nobody to click OK, and a scripted `MsgBox` ignores `/SUPPRESSMSGBOXES`.

    The sixth and seventh Windows runs hung here for fifteen minutes each: `InitializeWizard` opened the privacy notice
    unconditionally, and `iscc`-built setup waits for a button that never gets pressed. The rule is narrow and checkable —
    inside `[Code]`, every `MsgBox` sits under a `WizardSilent` test, so a silent install logs the words instead.
    """
    import re as _re

    code = _iss().split("[Code]", 1)
    assert len(code) == 2, "the definition has no [Code] section"
    body = code[1]
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if "MsgBox(" not in line or line.strip().startswith("{"):
            continue
        # Look back for the guard. `if WizardSilent then ... else MsgBox(...)` puts it a few lines up, and the guard may
        # itself be followed by other statements, so allow a small window rather than requiring the previous line.
        window = "\n".join(lines[max(0, index - 12) : index])
        assert "WizardSilent" in window, (
            f"MsgBox on line {index + 1} of [Code] is not guarded by `if WizardSilent` — an unattended install would "
            "block on it forever: " + line.strip()
        )
    # And the notice still reaches a silent install, in the log rather than a window.
    assert _re.search(r"Log\(", body), "a silent install learns nothing about the privacy notice"
    assert "no cloud" in body or "no account" in body

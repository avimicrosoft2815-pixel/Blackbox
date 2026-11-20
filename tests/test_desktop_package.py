"""The Linux desktop package: built here, extracted here, and run from the extracted tree.

What this file can prove without a display: that the `.deb` is a real Debian package with the dependencies the window
needs, that it installs the launcher, the desktop entry, the icon and the MIME type, that the vendored `pywebview` is
importable from inside the package, and that the binary it installs actually runs. What it cannot prove, and does not
claim: that a window appeared on a desktop Linux machine. That needs a machine with `python3-gi` and
`gir1.2-webkit2-4.1`, and `docs/desktop.md` says so in the platform matrix.

The build needs network once (the `pywebview` wheel) and `dpkg-deb`; the test skips with a named reason when either is
missing rather than passing quietly.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "tools" / "package" / "linux" / "build_deb.sh"

pytestmark = pytest.mark.skipif(shutil.which("dpkg-deb") is None or shutil.which("dpkg") is None,
                                reason="no dpkg-deb on this machine: the Linux package cannot be built here")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("deb")
    done = subprocess.run(["sh", str(BUILD), "--out", str(out)], capture_output=True, text=True, timeout=900, cwd=ROOT)
    if done.returncode != 0:
        pytest.skip(f"the package build needs network for the pywebview wheel: {done.stderr[-400:]}")
    debs = list(out.glob("*.deb"))
    assert len(debs) == 1, f"expected one package, found {debs}"
    root = out / "root"
    root.mkdir()
    subprocess.run(["dpkg-deb", "-x", str(debs[0]), str(root)], check=True)
    return {"deb": debs[0], "root": root}


def _control(deb: Path) -> str:
    return subprocess.run(["dpkg-deb", "-I", str(deb)], capture_output=True, text=True, check=True).stdout


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(root / "usr" / "bin" / "blackbox"), *args], capture_output=True, text=True, timeout=300)


# ── the package is a package ─────────────────────────────────────────────────
def test_the_deb_declares_the_window_it_needs(built: dict[str, Path]) -> None:
    control = _control(built["deb"])
    assert "Package: blackbox" in control
    assert "Architecture: amd64" in control or "Architecture: arm64" in control
    for dependency in ("python3 (>= 3.11)", "python3-gi", "gir1.2-webkit2-4.1"):
        assert dependency in control, f"{dependency} missing from:\n{control}"
    assert "Description: local-first flight recorder" in control


def test_nothing_in_the_package_needs_the_network_to_install(built: dict[str, Path]) -> None:
    """A package whose install step downloads is a package that fails on the laptop that needs it most."""
    control = _control(built["deb"])
    assert "pip" not in control.replace("Description:", "")
    listing = subprocess.run(["dpkg-deb", "-c", str(built["deb"])], capture_output=True, text=True, check=True).stdout
    for line in listing.splitlines():
        if not line.strip():
            continue
        path = line.split()[-1]
        assert path.startswith("./usr/") or path == "./", f"the package writes outside /usr: {path}"
    assert "./usr/share/applications/blackbox.desktop" in listing
    assert "./usr/share/icons/hicolor/scalable/apps/blackbox.svg" in listing
    assert "./usr/share/mime/packages/blackbox-capsule.xml" in listing


def test_the_launcher_runs_from_the_extracted_tree(built: dict[str, Path]) -> None:
    root = built["root"]
    version = _run(root, "--version")
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("blackbox ") and "format v" in version.stdout
    help_text = _run(root, "--help")
    assert help_text.returncode == 0
    status = _run(root, "status")
    assert status.returncode == 0 and "pre-alpha" in status.stdout


def test_the_vendored_window_backend_imports_inside_the_package(built: dict[str, Path]) -> None:
    """The point of vendoring: `pywebview` is importable from the installed tree, not from a developer's venv."""
    done = _run(built["root"], "desktop", "--selftest", "--json")
    report = json.loads(done.stdout)
    assert report["window_backend"] == "pywebview", report
    # GTK is a distribution package and is absent on the build machine; the report has to say so rather than pretend.
    if not report["gtk_ready"]:
        assert "python3-gi + gir1.2-webkit2-4.1" in report["missing"]
        assert done.returncode == 3
        assert "blackbox-recorder[desktop]" in report["install_hint"]


def test_the_desktop_entry_launches_the_window_and_claims_the_capsule_type(built: dict[str, Path]) -> None:
    entry = (built["root"] / "usr" / "share" / "applications" / "blackbox.desktop").read_text(encoding="utf-8")
    assert "Type=Application" in entry
    assert "Exec=blackbox desktop %f" in entry
    assert "MimeType=application/x-blackbox-capsule;" in entry
    assert "Icon=blackbox" in entry
    assert "Terminal=false" in entry, "a desktop app that opens a terminal is not a desktop app"

    # `defusedxml` is not a dependency of this project and this file is ours: the parser only sees what the build
    # script just wrote (S314 is about untrusted input).
    mime = ET.parse(built["root"] / "usr" / "share" / "mime" / "packages" / "blackbox-capsule.xml").getroot()  # noqa: S314
    types = [element.get("type") for element in mime.iter() if element.tag.endswith("mime-type")]
    assert types == ["application/x-blackbox-capsule"], types
    assert (built["root"] / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "blackbox.svg").exists()


def test_the_icon_is_the_product_black_and_gold(built: dict[str, Path]) -> None:
    svg = (built["root"] / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "blackbox.svg").read_text()
    assert svg.startswith("<?xml")
    assert "#e6c46a" in svg and "#0b0b0d" in svg, "the icon must use the product's own palette"
    ET.fromstring(svg)                                    # noqa: S314 - our own file; see above

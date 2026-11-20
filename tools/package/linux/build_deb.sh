#!/bin/sh
# Build an installable Debian package for the BlackBox desktop app.
#
#   tools/package/linux/build_deb.sh [--out DIR] [--version V]
#
# What this produces, and what it does not promise:
#
# * one `.deb` that installs `/usr/bin/blackbox`, the package under `/usr/lib/blackbox/`, a `.desktop` launcher, a
#   scalable icon and a MIME type for `.bbx` capsules;
# * `Depends: python3 (>= 3.11), python3-gi, gir1.2-webkit2-4.1` — the first because the core is stdlib-only, the other
#   two because the desktop **window** embeds the system webview (WebKitGTK) and those are distribution packages, not
#   wheels. A machine without them still gets the CLI, `view`, `serve` and every other command;
# * `pywebview` is vendored into `/usr/lib/blackbox/vendor`, because no Debian package provides it. It is used only by
#   `blackbox desktop`; the core never imports it (`tests/test_stdlib_only.py`).
#
# The build needs network **once**, to download the `pywebview` wheel, and `dpkg-deb`. The resulting package installs
# offline. Signing is not done here: that needs the owner's key, and `docs/desktop.md` says so where it matters.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$root"

out=dist
version=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    *) echo "build_deb: unknown argument $1" >&2; exit 2 ;;
  esac
done

arch=$(dpkg --print-architecture 2>/dev/null || echo amd64)
if [ -z "$version" ]; then
  version=$(python3 -c 'import tomllib,sys; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
fi
# Debian versions cannot carry an upstream "dev0" suffix with a dot-free shape we like; keep it, it is legal (0.1.0.dev0
# sorts before 0.1.0), and note it so a reader does not think this is a release.
name="blackbox_${version}_${arch}.deb"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "== 1/6 wheel: blackbox $version"
mkdir -p "$work/wheel"
python3 -m pip wheel . --no-deps --quiet --wheel-dir "$work/wheel"

echo "== 2/6 vendor: pywebview (the window, and only the window)"
# Not `--no-deps`: pywebview needs bottle/proxy-tools/typing-extensions at import time, and a package that installs an
# import-time failure is worse than one that installs nothing. The markers resolve for *this* platform, which is the one
# the package is for.
python3 -m pip download --quiet --only-binary=:all: pywebview -d "$work/vendor"

echo "== 3/6 stage the package tree"
pkg="$work/pkg"
mkdir -p "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/usr/lib/blackbox/vendor" \
         "$pkg/usr/share/applications" "$pkg/usr/share/icons/hicolor/scalable/apps" \
         "$pkg/usr/share/mime/packages" "$pkg/usr/share/doc/blackbox"
python3 -m pip install --quiet --no-index --find-links "$work/wheel" --target "$pkg/usr/lib/blackbox" \
        "blackbox-recorder==${version}"
python3 -m pip install --quiet --no-index --find-links "$work/vendor" --target "$pkg/usr/lib/blackbox/vendor" pywebview

install -m 0644 debian/blackbox.desktop "$pkg/usr/share/applications/blackbox.desktop"
install -m 0644 debian/blackbox.svg "$pkg/usr/share/icons/hicolor/scalable/apps/blackbox.svg"
install -m 0644 debian/blackbox-mime.xml "$pkg/usr/share/mime/packages/blackbox-capsule.xml"
install -m 0644 docs/desktop.md "$pkg/usr/share/doc/blackbox/desktop.md"

# The launcher finds its own package by walking up from the binary it was started as, so the same file works from
# `/usr/bin` and from a staged tree a test extracted with `dpkg-deb -x`.
cat > "$pkg/usr/bin/blackbox" <<'LAUNCHER'
#!/bin/sh
# `blackbox` — the desktop app's front door. The library lives beside this binary (../lib/blackbox), so a staged or
# relocated install works without an environment variable.
self=$(readlink -f -- "$0")
prefix=$(dirname -- "$(dirname -- "$self")")
lib="$prefix/lib/blackbox"
if [ ! -d "$lib/blackbox" ]; then
  echo "blackbox: cannot find its library under $lib" >&2
  exit 127
fi
exec /usr/bin/python3 -c 'import sys; sys.path[:0] = [sys.argv[1], sys.argv[1] + "/vendor"]; from blackbox.__main__ import main; raise SystemExit(main(sys.argv[2:]))' "$lib" "$@"
LAUNCHER
chmod 0755 "$pkg/usr/bin/blackbox"

installed_size=$(du -sk "$pkg/usr" | cut -f1)
cat > "$pkg/DEBIAN/control" <<CONTROL
Package: blackbox
Version: ${version}
Section: devel
Priority: optional
Architecture: ${arch}
Installed-Size: ${installed_size}
Maintainer: BlackBox contributors <noreply@example.invalid>
Description: local-first flight recorder for programs that call an LLM
 BlackBox records what a program does around its model calls into a crash-durable local tape, seals
 each run into a portable capsule, and opens that capsule in an offline black-and-gold viewer that
 runs no network request of any kind. This package installs the CLI and the desktop window; the window
 embeds the system WebKitGTK (python3-gi + gir1.2-webkit2-4.1) and needs no account, no server and no
 cloud service.
Depends: python3 (>= 3.11), python3-gi, gir1.2-webkit2-4.1
Recommends: xdg-utils
CONTROL

# No postinst beyond the caches a desktop file needs, and every step is best-effort: a package that fails to install
# because a container has no icon theme is a package that cannot be installed in a container.
cat > "$pkg/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
if command -v update-mime-database >/dev/null 2>&1; then update-mime-database /usr/share/mime || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true; fi
exit 0
POSTINST
chmod 0755 "$pkg/DEBIAN/postinst"

echo "== 4/6 build $name"
mkdir -p "$out"
dpkg-deb --build --root-owner-group "$pkg" "$out/$name" >/dev/null

echo "== 5/6 what is inside"
dpkg-deb -I "$out/$name" | sed -n '1,12p'
echo "-- entries: $(dpkg-deb -c "$out/$name" | wc -l), size: $(du -h "$out/$name" | cut -f1)"

echo "== 6/6 done: $out/$name"
echo "install with: sudo apt install ./$out/$name"
echo "verify with:  dpkg-deb -c $out/$name | head   and   tests/test_desktop_package.py"

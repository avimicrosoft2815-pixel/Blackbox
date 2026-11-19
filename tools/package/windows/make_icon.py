#!/usr/bin/env python3
"""Draw `blackbox.ico` from the same primitives as `debian/blackbox.svg`, with no image library and no network.

Why this exists: `blackbox.iss` names `blackbox.ico` in three places — the file list, `UninstallDisplayIcon`, and the
shortcut — and the repository did not contain one. `iscc` fails on the first missing `Source:`, so the Windows definition
was broken before it was ever compiled; `tests/test_windows_definition.py` now checks that every file the definition names
exists, which is what found it (defect 90).

The drawing is the product's own mark: a near-black rounded square, a gold ring, a gold waveform. It is rasterised here
with 4×4 supersampling per pixel so the small sizes are not jagged, and written as a PNG-in-ICO container (Vista and
later read PNG-compressed entries; the 16 and 32 pixel entries are also PNG, which Explorer accepts for sizes above 8).
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

FIELD = (11, 11, 13)          # #0b0b0d
GOLD_RING = (200, 160, 74)    # #c8a04a
GOLD_WAVE = (230, 196, 106)   # #e6c46a
SIZES = (16, 32, 48, 64, 128, 256)
#: The waveform from the SVG, in its 64-unit coordinate space.
WAVE = [(14, 36), (20, 36), (24, 26), (30, 44), (36, 20), (42, 40), (46, 32), (50, 32)]
SUPERSAMPLE = 4


def _rounded_rect_distance(x: float, y: float, half: float, radius: float) -> float:
    """Signed distance from (x, y) to a rounded square centred on the origin. Negative is inside."""
    dx, dy = abs(x) - (half - radius), abs(y) - (half - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    inside = min(max(dx, dy), 0.0)
    return outside + inside - radius


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / length2))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _wave_distance(x: float, y: float) -> float:
    return min(_segment_distance(x, y, *WAVE[i], *WAVE[i + 1]) for i in range(len(WAVE) - 1))


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    """One subpixel in 64-unit space, painted in order: field, ring, waveform.

    The rounded squares are described around the origin, so the point is moved to the middle of the 64-unit box first —
    the first version forgot that and drew the field into the top-left quadrant, which the icon test caught by sampling
    a pixel near the top edge and finding it transparent.
    """
    colour = (0, 0, 0, 0)
    cx, cy = x - 32.0, y - 32.0
    if _rounded_rect_distance(cx, cy, 30.0, 14.0) <= 0.0:
        colour = (*FIELD, 255)
        if abs(_rounded_rect_distance(cx, cy, 29.25, 13.4)) <= 0.75:
            colour = (*GOLD_RING, 255)
    if _wave_distance(x, y) <= 1.5:
        colour = (*GOLD_WAVE, 255)
    return colour


def _render(size: int) -> bytes:
    rows = []
    scale = 64.0 / size
    steps = [(index + 0.5) / SUPERSAMPLE for index in range(SUPERSAMPLE)]
    for py in range(size):
        row = bytearray()
        for px in range(size):
            red = green = blue = alpha = 0
            for sy in steps:
                for sx in steps:
                    r, g, b, a = _sample((px + sx) * scale, (py + sy) * scale)
                    if a:
                        red += r * a
                        green += g * a
                        blue += b * a
                        alpha += a
            total = SUPERSAMPLE * SUPERSAMPLE
            if alpha:
                row += bytes((round(red / alpha), round(green / alpha), round(blue / alpha),
                              round(alpha / total)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return _png(size, rows)


def _png(size: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)      # 8-bit RGBA, no interlace
    raw = b"".join(b"\x00" + row for row in rows)                   # filter 0 per scanline
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def build(dest: Path, sizes: tuple[int, ...] = SIZES) -> Path:
    images = [(size, _render(size)) for size in sizes]
    count = len(images)
    offset = 6 + 16 * count
    directory = bytearray(struct.pack("<HHH", 0, 1, count))        # reserved, type 1 = icon, count
    blobs = bytearray()
    for size, png in images:
        edge = 0 if size >= 256 else size                            # 0 means 256 in the directory entry
        directory += struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    dest.write_bytes(bytes(directory + blobs))
    return dest


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("blackbox.ico"))
    written = build(target)
    print(f"wrote {written} ({written.stat().st_size} bytes, {len(SIZES)} sizes: {', '.join(map(str, SIZES))})")

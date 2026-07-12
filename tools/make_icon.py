"""Generate the app icons — accent-coloured rounded square with the white
bridge arc.

Outputs (all committed, consumed by installer shortcuts and the web manifest):
    gui/static/app.ico       256px PNG-in-ICO (Vista+), shortcut icon
    gui/static/app-192.png   manifest icon
    gui/static/app-512.png   manifest icon

Stdlib only (hand-rolled PNG encoder). Run once and commit the output.
"""

import struct
import zlib
from pathlib import Path

ACCENT = (37, 99, 235)       # #2563EB — blue, the new default accent (2026-07-12)

# Bridge glyph in the 32px viewBox of the app's SVG:
# arc  M4 22 c 3.5 -8, 20.5 -8, 24 0   posts M4 22 v-4  M28 22 v-4
ARC32 = ((4, 22), (7.5, 14), (24.5, 14), (28, 22))
POSTS32 = (((4, 22), (4, 18)), ((28, 22), (28, 18)))
CORNER32 = 7
STROKE32 = 3


def cubic_points(p0, p1, p2, p3, n=240):
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = (mt**3 * p0[0] + 3 * mt**2 * t * p1[0]
             + 3 * mt * t**2 * p2[0] + t**3 * p3[0])
        y = (mt**3 * p0[1] + 3 * mt**2 * t * p1[1]
             + 3 * mt * t**2 * p2[1] + t**3 * p3[1])
        pts.append((x, y))
    return pts


def seg_points(a, b, n=40):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
            for i in range(n + 1)]


def render(size):
    f = size / 32
    glyph = cubic_points(*[(x * f, y * f) for x, y in ARC32])
    for a, b in POSTS32:
        glyph += seg_points((a[0] * f, a[1] * f), (b[0] * f, b[1] * f))
    corner = CORNER32 * f
    stroke = STROKE32 * f

    def glyph_dist(x, y):
        return min((x - gx) ** 2 + (y - gy) ** 2 for gx, gy in glyph) ** 0.5

    def rect_coverage(x, y):
        dx = max(corner - x, x - (size - 1 - corner), 0)
        dy = max(corner - y, y - (size - 1 - corner), 0)
        d = (dx * dx + dy * dy) ** 0.5 - corner
        return max(0.0, min(1.0, 0.5 - d / 1.5))

    def stroke_coverage(d):
        return max(0.0, min(1.0, (stroke / 2 + 0.75 - d) / 1.5))

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            bg = rect_coverage(x, y)
            if bg <= 0:
                row += b"\x00\x00\x00\x00"
                continue
            g = stroke_coverage(glyph_dist(x, y))
            r = int(ACCENT[0] + (255 - ACCENT[0]) * g)
            gc = int(ACCENT[1] + (255 - ACCENT[1]) * g)
            b = int(ACCENT[2] + (255 - ACCENT[2]) * g)
            row += bytes((r, gc, b, int(255 * bg)))
        rows.append(bytes(row))
    return rows


def png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data)))


def to_png(rows, size):
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(raw, 9))
            + png_chunk(b"IEND", b""))


def to_ico(png, size):
    header = struct.pack("<HHH", 0, 1, 1)
    wh = 0 if size >= 256 else size
    entry = struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


if __name__ == "__main__":
    static = Path(__file__).resolve().parents[1] / "gui" / "static"
    for size in (192, 512):
        png = to_png(render(size), size)
        (static / f"app-{size}.png").write_bytes(png)
        print(f"wrote app-{size}.png ({len(png)} bytes)")
    ico = to_ico(to_png(render(256), 256), 256)
    (static / "app.ico").write_bytes(ico)
    print(f"wrote app.ico ({len(ico)} bytes)")

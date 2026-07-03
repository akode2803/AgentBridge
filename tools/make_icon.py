"""Generate gui/static/app.ico — orange rounded square with the white bridge arc.

Stdlib only (hand-rolled PNG encoder + PNG-in-ICO container, Vista+ format).
Run once and commit the output; shortcuts created by the installer point at it.
"""

import struct
import zlib
from pathlib import Path

SIZE = 256
ACCENT = (216, 59, 1)        # #D83B01
CORNER_R = 56                # rx 7 in the 32px viewBox, scaled x8
STROKE = 24                  # stroke-width 3, scaled x8

# Bridge glyph from the app's SVG (viewBox 32, scaled x8):
# arc  M4 22 c 3.5 -8, 20.5 -8, 24 0   posts M4 22 v-4  M28 22 v-4
ARC = ((32, 176), (60, 112), (196, 112), (224, 176))
POSTS = (((32, 176), (32, 144)), ((224, 176), (224, 144)))


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


GLYPH = cubic_points(*ARC)
for a, b in POSTS:
    GLYPH += seg_points(a, b)


def glyph_dist(x, y):
    return min((x - gx) ** 2 + (y - gy) ** 2 for gx, gy in GLYPH) ** 0.5


def rect_coverage(x, y):
    """1 inside the rounded square, 0 outside, smooth 1.5px edge."""
    r = CORNER_R
    dx = max(r - x, x - (SIZE - 1 - r), 0)
    dy = max(r - y, y - (SIZE - 1 - r), 0)
    d = (dx * dx + dy * dy) ** 0.5 - r
    return max(0.0, min(1.0, 0.5 - d / 1.5))


def stroke_coverage(d):
    return max(0.0, min(1.0, (STROKE / 2 + 0.75 - d) / 1.5))


def render():
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
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


def to_png(rows):
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(raw, 9))
            + png_chunk(b"IEND", b""))


def to_ico(png):
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "gui" / "static" / "app.ico"
    out.write_bytes(to_ico(to_png(render())))
    print(f"wrote {out} ({out.stat().st_size} bytes)")

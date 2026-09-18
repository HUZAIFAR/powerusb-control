"""
Generate the PWA / home-screen icon: an amber power glyph on a dark tile.

Written as a tiny pure-Python PNG encoder so the project keeps its single
dependency (hidapi). Run it only if you want to change the artwork:

    python tools/make_icon.py
"""

import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

# 512 for the web app manifest; 180 is the size iOS actually wants for
# apple-touch-icon on iPhone/iPad, and letting iOS downscale a 512 gives a
# visibly softer home-screen icon.
SIZES = [("icon.png", 512), ("icon-180.png", 180)]
SS = 3                      # supersampling factor, for smooth edges

BG = (11, 13, 18)           # --bg-2
FG = (255, 179, 64)         # --amber


def coverage(px: float, py: float, SIZE: int) -> float:
    """
    How much of this point is 'ink', 0..1, for the power symbol:
    a ring with a gap at the top, plus a vertical bar through the gap.
    """
    cx = cy = SIZE / 2.0
    dx, dy = px - cx, py - cy
    dist = math.hypot(dx, dy)

    r_out, r_in = SIZE * 0.325, SIZE * 0.245
    in_ring = r_in <= dist <= r_out
    if in_ring:
        # Angle measured from straight up, 0..180 either side.
        ang = math.degrees(math.atan2(dx, -dy))
        if abs(ang) > 38:          # leave a gap at the top
            return 1.0

    bar_w = (r_out - r_in) / 2.0
    if abs(dx) <= bar_w and (cy - SIZE * 0.375) <= py <= cy - SIZE * 0.045:
        return 1.0
    return 0.0


def build_rows(SIZE: int) -> bytes:
    raw = bytearray()
    inv = 1.0 / (SS * SS)
    for y in range(SIZE):
        raw.append(0)                      # PNG filter type 0 for this row
        for x in range(SIZE):
            hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    if coverage(x + (sx + 0.5) / SS, y + (sy + 0.5) / SS, SIZE):
                        hits += 1
            a = hits * inv
            raw.extend(bytes(
                int(round(BG[i] + (FG[i] - BG[i]) * a)) for i in range(3)
            ))
    return bytes(raw)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def main() -> None:
    for name, size in SIZES:
        header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8-bit RGB
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", header)
               + chunk(b"IDAT", zlib.compress(build_rows(size), 9))
               + chunk(b"IEND", b""))
        out = WEB / name
        out.write_bytes(png)
        print(f"wrote {out} ({len(png)} bytes, {size}x{size})")


if __name__ == "__main__":
    main()

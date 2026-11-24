"""Draw the app icon (a planet with a moon on its orbit) as PNGs and an SVG, standard library only.

Not part of the app; the output is committed to orbit/web/static/icons/:

    uv run python scripts/build_icons.py

The drawing is full-bleed (iOS rounds the corners itself) and keeps everything inside the central
circle of 80% diameter, so the PNGs also work as Android "maskable" icons.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "orbit" / "web" / "static" / "icons"
SIZES = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}

BACKGROUND = (0x2F, 0x6F, 0xDF)  # --accent in app.css
FOREGROUND = (0xFF, 0xFF, 0xFF)

# Geometry as fractions of the icon size, centred at (0.5, 0.5).
PLANET_R = 0.13
ORBIT_R = 0.30
ORBIT_WIDTH = 0.04
MOON_R = 0.065
MOON_GAP = 0.035  # background ring around the moon, cutting the orbit
MOON_ANGLE = math.radians(-40)  # up and to the right


def _moon_centre() -> tuple[float, float]:
    return 0.5 + ORBIT_R * math.cos(MOON_ANGLE), 0.5 + ORBIT_R * math.sin(MOON_ANGLE)


def _coverage(distance: float) -> float:
    """Anti-aliased coverage of a pixel whose centre is `distance` pixels inside (>0) a shape."""
    return min(1.0, max(0.0, distance + 0.5))


def draw(size: int) -> list[bytes]:
    mx, my = _moon_centre()
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter type: none
        for x in range(size):
            px, py = (x + 0.5) / size, (y + 0.5) / size
            centre = math.hypot(px - 0.5, py - 0.5)
            moon = math.hypot(px - mx, py - my)
            planet = _coverage((PLANET_R - centre) * size)
            orbit = _coverage((ORBIT_WIDTH / 2 - abs(centre - ORBIT_R)) * size)
            gap = _coverage((MOON_R + MOON_GAP - moon) * size)
            moon_cov = _coverage((MOON_R - moon) * size)
            alpha = max(planet, orbit * (1 - gap), moon_cov)
            row += bytes(
                round(bg + (fg - bg) * alpha) for bg, fg in zip(BACKGROUND, FOREGROUND, strict=True)
            )
        rows.append(bytes(row))
    return rows


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    pixels = zlib.compress(b"".join(draw(size)), 9)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def svg() -> str:
    """The same drawing as a favicon, with rounded corners (browser tabs don't round them)."""
    mx, my = (round(v * 100, 2) for v in _moon_centre())
    bg = "#{:02x}{:02x}{:02x}".format(*BACKGROUND)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
        f'  <rect width="100" height="100" rx="22" fill="{bg}"/>\n'
        '  <mask id="m"><rect width="100" height="100" fill="#fff"/>'
        f'<circle cx="{mx}" cy="{my}" r="{(MOON_R + MOON_GAP) * 100:g}" fill="#000"/></mask>\n'
        f'  <circle cx="50" cy="50" r="{ORBIT_R * 100:g}" fill="none" stroke="#fff" '
        f'stroke-width="{ORBIT_WIDTH * 100:g}" mask="url(#m)"/>\n'
        f'  <circle cx="50" cy="50" r="{PLANET_R * 100:g}" fill="#fff"/>\n'
        f'  <circle cx="{mx}" cy="{my}" r="{MOON_R * 100:g}" fill="#fff"/>\n'
        "</svg>\n"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        (OUT_DIR / name).write_bytes(png(size))
    (OUT_DIR / "icon.svg").write_text(svg(), encoding="utf-8", newline="\n")
    print(f"wrote {', '.join([*SIZES, 'icon.svg'])} to {OUT_DIR}")


if __name__ == "__main__":
    main()

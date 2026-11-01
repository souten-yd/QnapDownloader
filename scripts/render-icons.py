#!/usr/bin/env python3
"""Render first-party app icons at QDK build time (not on the NAS).

Uses the checked-in SVG as the single source of truth; no external URLs,
downloads or build-time network requests. QNAP App Center expects files in
QDK's icons/ directory; iOS Safari expects apple-touch-icon.png in web/.
"""
from pathlib import Path
import sys

try:
    import cairosvg
    from PIL import Image
except ImportError as exc:
    raise SystemExit("Build dependency missing: pip install cairosvg pillow") from exc

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "web" / "icon.svg"


def render(web_dir, qpkg_icons):
    data = SVG.read_bytes()
    web_dir = Path(web_dir)
    qpkg_icons = Path(qpkg_icons)
    web_dir.mkdir(parents=True, exist_ok=True)
    qpkg_icons.mkdir(parents=True, exist_ok=True)

    outputs = (
        (web_dir / "apple-touch-icon.png", 180),
        (web_dir / "icon-192.png", 192),
        (qpkg_icons / "QnapDownloader.png", 256),
        (qpkg_icons / "QnapDownloader_80.png", 80),
    )
    for path, size in outputs:
        cairosvg.svg2png(bytestring=data, write_to=str(path),
                         output_width=size, output_height=size)
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.size != (size, size):
                raise ValueError("unexpected icon size: " + str(path))
    with Image.open(qpkg_icons / "QnapDownloader_80.png") as image:
        image.convert("L").convert("RGBA").save(
            qpkg_icons / "QnapDownloader_gray.png", optimize=True)
    print("QNAP/iOS icons generated and validated")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: render-icons.py WEB_DIR QPKG_ICONS_DIR")
    render(sys.argv[1], sys.argv[2])

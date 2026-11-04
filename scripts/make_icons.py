"""Regenerates cts/assets/cts.png, cts.ico and cts.icns (run once; the results are committed).

Draws the same geometry as cts.svg with Pillow so no SVG renderer is needed.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "cts" / "assets"
S = 1024


def draw(size: int) -> Image.Image:
    scale = 4  # supersample for smooth edges
    big = S * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bg = (11, 11, 11)

    def rr(x, y, w, h, r, fill, outline=None, width=0, alpha=255):
        # Blend against the background by hand: ImageDraw on RGBA *replaces* pixels, which would
        # punch translucent holes in the icon instead of tinting it.
        a = alpha / 255
        color = tuple(round(c * a + b * (1 - a)) for c, b in zip(fill, bg)) + (255,)
        d.rounded_rectangle([x * scale, y * scale, (x + w) * scale, (y + h) * scale], r * scale, fill=color,
                            outline=outline, width=width * scale)

    rr(64, 64, 896, 896, 224, (11, 11, 11), outline=(48, 48, 48), width=12)
    white = (250, 250, 250)
    rr(232, 296, 240, 200, 28, white)
    rr(232, 528, 240, 200, 28, white, alpha=140)
    rr(552, 332, 240, 40, 20, white)
    rr(552, 432, 240, 40, 20, white)
    rr(552, 532, 240, 40, 20, white, alpha=190)
    rr(552, 632, 150, 40, 20, white, alpha=115)
    return img.resize((size, size), Image.LANCZOS)


def build_icns(png_by_type: dict[bytes, bytes]) -> bytes:
    body = b"".join(t + struct.pack(">I", len(data) + 8) + data for t, data in png_by_type.items())
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def png_bytes(img: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    draw(512).save(ASSETS / "cts.png", optimize=True)
    base = draw(256)
    base.save(ASSETS / "cts.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icns = build_icns({
        b"ic07": png_bytes(draw(128)),   # 128x128
        b"ic08": png_bytes(draw(256)),   # 256x256
        b"ic09": png_bytes(draw(512)),   # 512x512
        b"ic10": png_bytes(draw(1024)),  # 1024x1024 (512@2x)
    })
    (ASSETS / "cts.icns").write_bytes(icns)
    for name in ("cts.png", "cts.ico", "cts.icns"):
        print(name, (ASSETS / name).stat().st_size, "bytes")


if __name__ == "__main__":
    main()

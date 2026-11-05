"""Render the overlay from an untouched original poster. Never reads a previously rendered poster."""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def load_font(path: str, size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(os.path.expanduser(path), size)
    try:  # variable fonts: pick the named weight instance; static fonts raise and are used as-is
        font.set_variation_by_name(weight)
    except (OSError, AttributeError, ValueError):
        pass
    return font


# Every poster we draw on carries this JPEG comment. A fresh data folder that meets a server poster with it knows that is one of
# OUR overlays (with the real original stored elsewhere) and refuses to adopt it as an "original", which would stack a second overlay.
MARKER = b"laminator-overlay:1"
RENDER_VERSION = "2"  # part of the "already applied" hash: bump it to make every poster re-render once


def is_overlaid(data: bytes) -> bool:
    return MARKER in data[:65536]


def render(original: bytes, text: str, tpl: dict) -> bytes:
    img = Image.open(io.BytesIO(original)).convert("RGB")
    w, h = img.size
    g, s = tpl["gradient"], tpl["score"]

    # bottom gradient: opacity eases from 0 (top of band) to max_opacity (bottom edge)
    band = max(1, int(h * g["height"]))
    mask = Image.new("L", (1, band))
    for y in range(band):
        t = y / (band - 1) if band > 1 else 1
        mask.putpixel((0, y), int(255 * g["max_opacity"] * t * t * (3 - 2 * t)))  # smoothstep
    mask = mask.resize((w, band))
    layer = Image.new("RGB", (w, band), tuple(g["color"]))
    img.paste(layer, (0, h - band), mask)

    font = load_font(s["font"], max(8, int(h * s["size"])), s.get("weight", "Bold"))
    draw = ImageDraw.Draw(img)
    sw = round(font.size * s.get("embolden", 0))  # outline in the text colour: fakes a heavier weight
    l, t, r, b = draw.textbbox((0, 0), text, font=font, stroke_width=sw)
    x = w - int(w * s["margin_x"]) - (r - l) - l
    y = h - int(h * s["margin_y"]) - (b - t) - t
    if s.get("shadow"):
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 200), stroke_width=sw, stroke_fill=(0, 0, 0, 200))
        sh = sh.filter(ImageFilter.GaussianBlur(max(1, h // 400)))
        img = Image.alpha_composite(img.convert("RGBA"), sh).convert("RGB")
        draw = ImageDraw.Draw(img)
    draw.text((x, y), text, font=font, fill=tuple(s["color"]), stroke_width=sw, stroke_fill=tuple(s["color"]))

    out = io.BytesIO()
    img.save(out, "JPEG", quality=92, comment=MARKER)
    return out.getvalue()

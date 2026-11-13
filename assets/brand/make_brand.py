"""Generate every Binday brand asset from one definition of the mark and wordmark.

    pip install pillow fonttools      (dev only; the app doesn't need them)
    python assets/brand/make_brand.py

Writes:
  assets/icon.png                 app icon used by `flet build` (1024, transparent margin)
  assets/brand/mark.svg           the mark, vector
  assets/brand/mark-256.png       the mark, raster (in-app rail)
  assets/brand/logo-light.svg     mark + wordmark for light backgrounds (README)
  assets/brand/logo-dark.svg      mark + wordmark for dark backgrounds (README)
  assets/brand/social-preview.png 1280x640 GitHub social preview (upload in repo Settings)
  assets/readme/banner-light.svg  README banner, light and dark (screenshots: assets/readme/capture.ps1)
  assets/readme/banner-dark.svg

The mark: a calendar day ("bin day") in Mattermost navy with a blue header and two binding
rings; below, the three bin bars sorted longest to shortest, like the app's load bar.
"""
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
ASSETS = HERE.parent
FONT = ASSETS / "fonts" / "PublicSans.ttf"

NAVY, BLUE, RING = "#1E325C", "#1C58D9", "#E8EEFA"
TEAL, YELLOW, BROWN = "#3FB8B5", "#E8B21A", "#C9956A"
INK, INK2, GROUND = "#182033", "#4B5568", "#F4F6FA"
DARK_INK, DARK_INK2 = "#E8ECF4", "#B0B9CB"

# geometry on a 1024 canvas
TILE = (64, 64, 960, 960, 200)                       # x0 y0 x1 y1 radius
HEADER_Y = 300                                       # header band bottom
RINGS = [(330, 112, 386, 232), (638, 112, 694, 232)]  # binding rings (x0 y0 x1 y1), fully rounded
BARS = [(208, 436, 816, TEAL), (208, 584, 664, YELLOW), (208, 732, 512, BROWN)]  # x0 y x1 color
BAR_H = 96


# ------------------------------------------------------------------ vector

def mark_svg_body(x=0, y=0, size=1024) -> str:
    s = size / 1024
    x0, y0, x1, y1, r = TILE
    g = [f'<g transform="translate({x} {y}) scale({s:.5f})">',
         '<defs><clipPath id="tile"><rect x="64" y="64" width="896" height="896" rx="200"/></clipPath></defs>',
         f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{NAVY}"/>',
         f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{HEADER_Y - y0}" fill="{BLUE}" clip-path="url(#tile)"/>']
    for rx0, ry0, rx1, ry1 in RINGS:
        g.append(f'<rect x="{rx0}" y="{ry0}" width="{rx1 - rx0}" height="{ry1 - ry0}" rx="{(rx1 - rx0) / 2}" fill="{RING}"/>')
    for bx0, by, bx1, col in BARS:
        g.append(f'<rect x="{bx0}" y="{by}" width="{bx1 - bx0}" height="{BAR_H}" rx="{BAR_H / 2}" fill="{col}"/>')
    g.append("</g>")
    return "".join(g)


def _font(weight):
    f = TTFont(FONT)
    return instancer.instantiateVariableFont(f, {"wght": weight})


def text_path(text, weight, size, x, baseline, tracking=0.0):
    """SVG path data for text, outlines from Public Sans; returns (d, advance)."""
    f = _font(weight)
    upm = f["head"].unitsPerEm
    gs, cmap, hmtx = f.getGlyphSet(), f.getBestCmap(), f["hmtx"]
    k = size / upm
    pen = SVGPathPen(gs)
    cx = 0.0
    for ch in text:
        name = cmap[ord(ch)]
        gs[name].draw(TransformPen(pen, (k, 0, 0, -k, x + cx, baseline)))
        cx += hmtx[name][0] * k + tracking * size
    return pen.getCommands(), cx - tracking * size


def logo_svg(dark: bool) -> str:
    ink, ink2 = (DARK_INK, DARK_INK2) if dark else (NAVY, INK2)
    h = 120
    word, ww = text_path("binday", 800, 78, 136, 80, tracking=-0.02)
    desc, dw = text_path("for Mattermost", 500, 22, 140, 110)
    w = int(136 + max(ww, dw) + 8)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="Binday for Mattermost">{mark_svg_body(0, 6, 108)}'
            f'<path d="{word}" fill="{ink}"/><path d="{desc}" fill="{ink2}"/></svg>')


def banner_svg(dark: bool) -> str:
    """1280x320 README banner: mark, wordmark, descriptor, tagline, and the load bar along the bottom."""
    bg, ink, ink2, ink3 = ("#0F131C", DARK_INK, DARK_INK2, DARK_INK2) if dark else (GROUND, NAVY, INK2, INK)
    edge = "#2C3546" if dark else "#D5DBE5"
    word, _ = text_path("binday", 800, 112, 300, 166, tracking=-0.02)
    desc, _ = text_path("for Mattermost", 500, 32, 306, 214)
    tag, _ = text_path("See what's taking space, choose what goes, and delete it safely.", 400, 25, 306, 262)
    bars, cx = [], 64
    for frac, col in ((0.36, TEAL), (0.22, YELLOW), (0.42, BROWN)):
        w = int(1152 * frac) - 6
        bars.append(f'<rect x="{cx}" y="290" width="{w}" height="8" rx="4" fill="{col}"/>')
        cx += w + 6
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 320" width="1280" height="320" role="img" '
            f'aria-label="Binday for Mattermost: see what\'s taking space, choose what goes, and delete it safely.">'
            f'<rect x="0.5" y="0.5" width="1279" height="319" rx="24" fill="{bg}" stroke="{edge}"/>'
            f'{mark_svg_body(64, 52, 196)}<path d="{word}" fill="{ink}"/><path d="{desc}" fill="{ink2}"/>'
            f'<path d="{tag}" fill="{ink3}"/>{"".join(bars)}</svg>')


# ------------------------------------------------------------------ raster

def mark_image(size: int) -> Image.Image:
    ss = 4
    S = 1024 * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1, r = [v * ss for v in TILE]
    tile_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle((x0, y0, x1, y1), r, fill=255)
    layer = Image.new("RGBA", (S, S), NAVY)
    ImageDraw.Draw(layer).rectangle((x0, y0, x1, HEADER_Y * ss), fill=BLUE)
    img.paste(layer, (0, 0), tile_mask)
    for rx0, ry0, rx1, ry1 in RINGS:
        d.rounded_rectangle((rx0 * ss, ry0 * ss, rx1 * ss, ry1 * ss), (rx1 - rx0) * ss // 2, fill=RING)
    for bx0, by, bx1, col in BARS:
        d.rounded_rectangle((bx0 * ss, by * ss, bx1 * ss, (by + BAR_H) * ss), BAR_H * ss // 2, fill=col)
    return img.resize((size, size), Image.LANCZOS)


def pil_font(weight, size):
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight])
    return f


def social_preview() -> Image.Image:
    W, H = 1280, 640
    img = Image.new("RGB", (W, H), GROUND)
    d = ImageDraw.Draw(img)
    m = mark_image(300)
    img.paste(m, (120, (H - 300) // 2), m)
    x = 480
    d.text((x, 200), "binday", font=pil_font(800, 132), fill=NAVY)
    d.text((x + 6, 352), "for Mattermost", font=pil_font(500, 40), fill=INK2)
    d.text((x + 6, 432), "See what's taking space, choose what goes,", font=pil_font(400, 30), fill=INK)
    d.text((x + 6, 474), "and delete it safely.", font=pil_font(400, 30), fill=INK)
    # the load bar, as a quiet signature along the bottom edge
    y, h, gap = H - 28, 10, 6
    total, cx = W - 240, 120
    for frac, col in ((0.36, TEAL), (0.22, YELLOW), (0.42, BROWN)):
        wpx = int(total * frac) - gap
        d.rounded_rectangle((cx, y, cx + wpx, y + h), 4, fill=col)
        cx += wpx + gap
    return img


def main():
    (HERE / "mark.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="Binday">{mark_svg_body()}</svg>',
        encoding="utf-8")
    (HERE / "logo-light.svg").write_text(logo_svg(dark=False), encoding="utf-8")
    (HERE / "logo-dark.svg").write_text(logo_svg(dark=True), encoding="utf-8")
    readme = ASSETS / "readme"
    readme.mkdir(exist_ok=True)
    (readme / "banner-light.svg").write_text(banner_svg(dark=False), encoding="utf-8")
    (readme / "banner-dark.svg").write_text(banner_svg(dark=True), encoding="utf-8")
    mark_image(1024).save(ASSETS / "icon.png", optimize=True)
    mark_image(256).save(HERE / "mark-256.png", optimize=True)
    social_preview().save(HERE / "social-preview.png", optimize=True)
    for p in sorted([ASSETS / "icon.png", *HERE.glob("*.svg"), *HERE.glob("*.png")]):
        print(f"{p.relative_to(ASSETS.parent)}  {p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()

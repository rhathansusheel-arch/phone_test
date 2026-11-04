"""Cover maker: full wrap covers (back + spine + front) as print-ready PDFs.

Paperback width = 0.125 bleed + back + spine + front + 0.125 bleed; height = bleed + trim + bleed.
Spine = pages x 0.002252 in (white paper). Spine text only if more than 79 pages.
Text stays at least 0.25 in from every trim edge and the spine folds.
The barcode area (bottom-right of the back) is left empty.
"""

from __future__ import annotations

import base64
import re
import html
import logging
from pathlib import Path

from pypdf import PdfReader

from . import pdftools, specs
from .interior import FONT_STACK, html_to_pdf

log = logging.getLogger("covers")

PALETTES = {
    "ocean":     ("#0b6fa4", "#3fc1e0", "#ffd23f"),
    "sea":       ("#0b6fa4", "#3fc1e0", "#ffd23f"),
    "vehicle":   ("#d7263d", "#f49d37", "#1b998b"),
    "truck":     ("#d7263d", "#f49d37", "#1b998b"),
    "car":       ("#d7263d", "#f49d37", "#1b998b"),
    "christmas": ("#b3001b", "#1f7a3a", "#ffd166"),
    "winter":    ("#2b59c3", "#9bd1e5", "#ffffff"),
    "dinosaur":  ("#3a7d44", "#9dc183", "#f4a259"),
    "farm":      ("#7cb518", "#fbb02d", "#e4572e"),
    "flower":    ("#c2185b", "#f48fb1", "#ffe082"),
    "animal":    ("#ef6f6c", "#f9c74f", "#43aa8b"),
    "space":     ("#22223b", "#4a4e69", "#f2e94e"),
    "halloween": ("#ff6d00", "#3c096c", "#ffd60a"),
    "easter":    ("#9c89b8", "#f0a6ca", "#b8e0d2"),
}
DEFAULT_PALETTE = ("#5e3bee", "#00b4d8", "#ffbe0b")


def palette_for(text: str) -> tuple[str, str, str]:
    low = text.lower()
    for word, pal in PALETTES.items():
        if word in low:
            return pal
    return DEFAULT_PALETTE


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


def _nobreak(text: str) -> str:
    """Escape text and keep things like 'Ages 2-5' on one line."""
    return re.sub(r"(\w+ \d+\s?-\s?\d+\+?|\d+\s?-\s?\d+)", r'<span style="white-space:nowrap">\1</span>', html.escape(text))


def cover_html(size: dict, title: str, subtitle: str, pen_name: str, badge: str,
               blurb: str, pictures: list[Path], colours: tuple[str, str, str]) -> str:
    e = html.escape
    W, H = size["width"], size["height"]
    edge, tw, th, spine = size["edge"], size["trim_w"], size["trim_h"], size["spine"]
    hinge = size.get("hinge", 0.0)
    safe = float(specs.get()["paperback"]["cover_safe"])
    bw, bh = specs.get()["paperback"]["barcode_box"]
    back_x, spine_x = edge, edge + tw
    front_x = edge + tw + spine
    top = edge + safe
    # Front safe box (inches, from page top-left)
    fx0, fx1 = front_x + safe + hinge, front_x + tw - safe
    fy0, fy1 = top, edge + th - safe
    fw, fh = fx1 - fx0, fy1 - fy0
    # Back safe box
    bx0, bx1 = back_x + safe, back_x + tw - safe - hinge
    bww = bx1 - bx0
    c1, c2, c3 = colours
    pics = [_data_uri(p) for p in pictures[:3]]
    title_size = min(0.75, fw / max(8, len(title) * 0.55) * 1.9)
    main_pic = pics[0] if pics else ""
    side_pics = pics[1:3]
    side_html = "".join(
        f'<div class="tile small" style="transform:rotate({r}deg)"><img src="{p}"></div>'
        for p, r in zip(side_pics, (-6, 5)))
    back_pics = "".join(f'<div class="tile mini"><img src="{p}"></div>' for p in pics[:3])
    spine_html = ""
    if size["spine_text"]:
        font = max(0.08, min(0.3, spine - 0.125))
        spine_html = f"""<div class="spine" style="left:{spine_x}in;width:{spine}in;top:{edge}in;height:{th}in">
          <div class="spine-text" style="font-size:{font:.3f}in">{e(title)}&nbsp;&nbsp;•&nbsp;&nbsp;{e(pen_name)}</div></div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: {W}in {H}in; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: 100%; height: 100%; overflow: hidden; font-family: {FONT_STACK}; }}
.bg {{ position: absolute; inset: 0; background: linear-gradient(160deg, {c2} 0%, {c1} 70%); }}
.dots {{ position: absolute; inset: 0; background-image: radial-gradient(rgba(255,255,255,.18) 0.06in, transparent 0.07in);
         background-size: 0.6in 0.6in; }}
.box {{ position: absolute; display: flex; flex-direction: column; align-items: center; text-align: center; }}
.title-panel {{ background: #fff; border-radius: 0.3in; padding: 0.22in 0.25in; width: 100%;
                box-shadow: 0 0.06in 0 rgba(0,0,0,.25); border: 0.05in solid {c3}; }}
h1 {{ font-size: {title_size:.3f}in; line-height: 1.05; color: {c1}; font-weight: 900; }}
h2 {{ font-size: {min(0.26, fw / 26):.3f}in; line-height: 1.25; color: #222; font-weight: 600; margin-top: 0.12in; }}
.tile {{ background: #fff; border-radius: 0.25in; padding: 0.15in; box-shadow: 0 0.05in 0.12in rgba(0,0,0,.3); }}
.tile img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
.big {{ width: {fw * 0.72:.3f}in; height: {fh * 0.42:.3f}in; }}
.small {{ width: {fw * 0.36:.3f}in; height: {fh * 0.2:.3f}in; }}
.mini {{ width: {bww * 0.3:.3f}in; height: {bww * 0.3:.3f}in; }}
.badge {{ position: absolute; width: 1.25in; height: 1.25in; border-radius: 50%; background: {c3};
          color: #111; font-weight: 900; font-size: 0.24in; display: flex; align-items: center; justify-content: center;
          text-align: center; line-height: 1.05; border: 0.05in solid #fff; transform: rotate(-10deg); }}
.author {{ color: #fff; font-size: 0.3in; font-weight: 800; letter-spacing: 0.04em; text-shadow: 0 0.03in 0 rgba(0,0,0,.35); }}
.blurb {{ width: 100%; background: rgba(255,255,255,.95); border-radius: 0.25in; padding: 0.3in; color: #111; font-size: 0.17in;
          line-height: 1.45; text-align: left; white-space: pre-line; }}
.row {{ display: flex; gap: 0.15in; justify-content: center; }}
.spine {{ position: absolute; display: flex; align-items: center; justify-content: center; }}
.spine-text {{ writing-mode: vertical-rl; color: #fff; font-weight: 800; white-space: nowrap; }}
</style></head><body>
<div class="bg"></div><div class="dots"></div>
<div class="box" style="left:{fx0}in;top:{fy0}in;width:{fw}in;height:{fh}in;justify-content:space-between">
  <div class="title-panel"><h1>{_nobreak(title)}</h1>{f'<h2>{_nobreak(subtitle)}</h2>' if subtitle else ''}</div>
  <div class="tile big">{f'<img src="{main_pic}">' if main_pic else ''}</div>
  <div class="row">{side_html}</div>
  <div class="author">{e(pen_name)}</div>
</div>
<div class="badge" style="left:{fx1 - 1.3:.3f}in;top:{fy0 + fh * 0.36:.3f}in">{e(badge)}</div>
<div class="box" style="left:{bx0}in;top:{top}in;width:{bww}in;gap:0.3in">
  <div class="blurb">{e(blurb)}</div>
  <div class="row">{back_pics}</div>
</div>
{spine_html}
<!-- barcode area: {bw} x {bh} in at bottom-right of the back cover, kept empty -->
</body></html>"""


def barcode_area(size: dict) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) in inches from the page's top-left."""
    bw, bh = specs.get()["paperback"]["barcode_box"]
    safe = float(specs.get()["paperback"]["cover_safe"])
    right = size["edge"] + size["trim_w"] - safe - size.get("hinge", 0.0)
    bottom = size["edge"] + size["trim_h"] - safe
    return (right - bw, bottom - bh, right, bottom)


async def make_cover(fmt: str, trim: str, pages: int, out: Path, title: str, subtitle: str,
                     pen_name: str, badge: str, blurb: str, pictures: list[Path],
                     theme: str = "", calculator: dict | None = None, palette: list | None = None) -> dict:
    """Build the cover PDF. calculator = sizes read from KDP's cover calculator (wins over the formula)."""
    size = specs.paperback_cover_size(trim, pages) if fmt == "paperback" else specs.hardcover_cover_size(trim, pages)
    if calculator:
        size.update({k: calculator[k] for k in ("width", "height", "spine") if k in calculator})
        if fmt == "hardcover":
            size["edge"] = round((size["width"] - 2 * size["trim_w"] - size["spine"]) / 2, 4)
        size["source"] = "KDP cover calculator"
    colours = palette_for(f"{theme} {title}")
    if palette and len(palette) == 3 and all(re.fullmatch(r"#[0-9a-fA-F]{6}", c or "") for c in palette):
        colours = tuple(palette)
    html_text = cover_html(size, title, subtitle, pen_name, badge, blurb, pictures, colours)
    extra = 0.05  # print a little oversized, then trim to the exact size (browsers round page sizes)
    html_text = html_text.replace(f"size: {size['width']}in {size['height']}in",
                                  f"size: {size['width'] + extra}in {size['height'] + extra}in")
    raw = out.with_suffix(".raw.pdf")
    await html_to_pdf(html_text, raw, size["width"] + extra, size["height"] + extra)
    _trim_to(raw, out, size["width"], size["height"])
    raw.unlink()
    check = verify_cover(out, size, calculator)
    return {"path": str(out), "size": size, "checks": check}


def _trim_to(src: Path, out: Path, w: float, h: float) -> None:
    """Set the page to exactly w x h inches, keeping the top-left corner where it is."""
    from pypdf import PdfWriter
    reader = PdfReader(str(src))
    page = reader.pages[0]
    top = float(page.mediabox.top)
    page.mediabox.lower_left = (0, top - h * 72)
    page.mediabox.upper_right = (w * 72, top)
    page.cropbox = page.mediabox
    page.trimbox = page.mediabox
    page.bleedbox = page.mediabox
    writer = PdfWriter()
    writer.add_page(page)
    writer.metadata = None
    with open(out, "wb") as f:
        writer.write(f)


def verify_cover(pdf: Path, size: dict, calculator: dict | None) -> list[dict]:
    reader = PdfReader(str(pdf))
    checks = []
    w_in = float(reader.pages[0].mediabox.width) / 72
    h_in = float(reader.pages[0].mediabox.height) / 72
    ok = len(reader.pages) == 1 and abs(w_in - size["width"]) < 0.01 and abs(h_in - size["height"]) < 0.01
    checks.append({"ok": ok, "text": f"Cover is {w_in:.3f} x {h_in:.3f} in (needs {size['width']:.3f} x {size['height']:.3f}, "
                                     f"spine {size['spine']:.3f} in, from {size['source']})"})
    if calculator:
        match = abs(w_in - calculator["width"]) < 0.02 and abs(h_in - calculator["height"]) < 0.02
        checks.append({"ok": match, "text": "Matches KDP's cover calculator" if match else
                       f"Does NOT match KDP's cover calculator ({calculator['width']} x {calculator['height']})"})
    else:
        checks.append({"ok": None, "text": "Not yet checked against KDP's cover calculator (done on your PC)"})
    # barcode area must be empty (plain background)
    img = pdftools.render(pdf, 0, 50, smooth=True)
    x0, y0, x1, y1 = (int(v * 50) for v in barcode_area(size))
    import numpy as np
    region = np.asarray(img.crop((x0, y0, x1, y1)), dtype=np.int16)
    spread = float(region.std(axis=(0, 1)).max()) if region.size else 0.0
    checks.append({"ok": spread < 30, "text": "Barcode area is empty" if spread < 30 else "Something is in the barcode area"})
    fonts = pdftools._fonts_not_embedded(reader)
    checks.append({"ok": not fonts, "text": "Fonts embedded" if not fonts else f"Fonts not embedded: {fonts}"})
    return checks


def front_preview(pdf: Path, size: dict, out: Path, dpi: int = 120) -> Path:
    """Just the front cover as a PNG (for Telegram)."""
    img = pdftools.render(pdf, 0, dpi, smooth=True)
    x0 = int((size["edge"] + size["trim_w"] + size["spine"]) * dpi)
    img = img.crop((x0, 0, img.width, img.height))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def wrap_preview(pdf: Path, out: Path, dpi: int = 60) -> Path:
    img = pdftools.render(pdf, 0, dpi, smooth=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ------------------------------------------------------------ KDP cover calculator (on your PC)

async def read_kdp_calculator(fmt: str, trim: str, pages: int) -> dict | None:
    """Fill KDP's public cover calculator with the research browser and read the sizes.
    Returns {"width", "height", "spine"} in inches, or None if the page couldn't be read."""
    import re
    from . import settings
    from .browser import goto, human_pause, profile_session
    base = settings.load_config()["browser"]["kdp_base_url"]
    w, h = specs.parse_trim(trim)
    async with profile_session("research") as ctx:
        page = await ctx.new_page()
        await goto(page, f"{base}/cover-calculator", "opening KDP's cover calculator")
        try:
            for label, value in [("Binding type", "Hardcover" if fmt == "hardcover" else "Paperback"),
                                 ("Interior type", "Black & white"), ("Paper type", "White"),
                                 ("Measurement units", "Inches")]:
                field = page.get_by_label(label, exact=False).first
                if await field.count():
                    await field.select_option(label=value)
                    await human_pause(0.5)
            trim_field = page.get_by_label("Trim size", exact=False).first
            options = await trim_field.locator("option").all_inner_texts()
            want = next((o for o in options if re.sub(r"[^0-9.x]", "", o.lower().replace(" ", "")).startswith(
                f"{w:g}x{h:g}")), None)
            if want is None:
                return None
            await trim_field.select_option(label=want)
            await page.get_by_label("Page count", exact=False).first.fill(str(pages))
            await human_pause(0.5)
            await page.get_by_role("button", name=re.compile("Calculate", re.I)).first.click()
            await human_pause()
            text = await page.inner_text("body")
        except Exception as exc:
            log.warning("Cover calculator could not be read: %s", exc)
            return None
    def grab(label):
        m = re.search(label + r"[^0-9]{0,40}([0-9]+\.[0-9]+)\s*(?:in|\")", text, re.I)
        return float(m.group(1)) if m else None
    width, height, spine = grab("width"), grab("height"), grab("spine")
    if not (width and height):
        return None
    return {"width": width, "height": height, "spine": spine}

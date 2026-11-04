"""Interior builder: turns pictures into a KDP-ready interior PDF.

Layout (no bleed):
  1 title page  2 blank  3 "This book belongs to"  4 copyright
  then every picture on a right-hand page with a blank back
  then a colour test page and its blank back.
Every picture is fitted inside safe margins (bigger than KDP's minimum gutter).
"""

from __future__ import annotations

import html
import io
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation

from . import pdftools, settings, specs
from .browser import plain_browser

log = logging.getLogger("interior")

FONT_STACK = "'Arial Rounded MT Bold', 'Trebuchet MS', 'Segoe UI', 'DejaVu Sans', Arial, sans-serif"


@dataclass
class Picture:
    pdf: Path
    index: int
    bbox_in: tuple | None = None       # ink box from the checker (inches, from bottom-left)
    rasterise: bool = False            # True when the page has fonts that aren't embedded


def safe_margin(total_pages: int) -> float:
    return max(0.5, specs.gutter(total_pages) + 0.125, specs.outside_margin() + 0.25)


def _page(inner: str) -> str:
    return f'<section class="page">{inner}</section>'


def front_matter_html(title: str, subtitle: str, pen_name: str, trim: str, margin: float) -> str:
    w, h = specs.parse_trim(trim)
    cfg = settings.load_config()
    copyright_text = cfg["books"]["copyright_text"].format(year=date.today().year, pen_name=pen_name)
    e = html.escape
    title_page = _page(f"""
      <div class="center">
        <h1>{e(title)}</h1>
        {f'<h2>{e(subtitle)}</h2>' if subtitle else ''}
        <p class="by">{e(pen_name)}</p>
      </div>""")
    blank = _page("")
    belongs = _page("""
      <div class="center">
        <div class="frame">
          <p class="belongs">This book belongs to</p>
          <div class="line"></div>
        </div>
      </div>""")
    copyright_page = _page(f"""
      <div class="bottom">
        <p>{e(copyright_text)}</p>
        <p>{e(cfg['books']['ai_note'])}</p>
        <p>Tip: put a sheet of paper or card behind the page you are colouring.
           Every picture has a blank back, so you can cut it out and keep it.</p>
      </div>""")
    return _doc(w, h, margin, title_page + blank + belongs + copyright_page)


def test_page_html(trim: str, margin: float) -> str:
    w, h = specs.parse_trim(trim)
    boxes = "".join('<div class="swatch"></div>' for _ in range(24))
    page = _page(f"""
      <h3>Test your colours here</h3>
      <div class="grid">{boxes}</div>""")
    return _doc(w, h, margin, page + _page(""))


def _doc(w: float, h: float, margin: float, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: {w}in {h}in; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #fff; color: #000; font-family: {FONT_STACK}; }}
.page {{ width: {w}in; height: {h}in; padding: {margin}in; position: relative; overflow: hidden;
         page-break-after: always; break-after: page; }}
.center {{ height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }}
h1 {{ font-size: {min(0.6, w / 12):.2f}in; line-height: 1.15; margin: 0 0 0.25in; }}
h2 {{ font-size: {min(0.26, w / 30):.2f}in; font-weight: normal; margin: 0 0 0.5in; }}
.by {{ font-size: 0.22in; letter-spacing: 0.05em; }}
.frame {{ border: 3pt solid #000; border-radius: 0.35in; padding: 0.6in 0.5in; width: 85%; }}
.belongs {{ font-size: 0.4in; margin: 0 0 0.9in; }}
.line {{ border-bottom: 2pt solid #000; width: 100%; }}
.bottom {{ position: absolute; bottom: {margin}in; left: {margin}in; right: {margin}in; font-size: 9pt; line-height: 1.5; }}
h3 {{ text-align: center; font-size: 0.32in; margin: 0.2in 0 0.35in; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.25in; }}
.swatch {{ border: 2pt solid #000; border-radius: 0.15in; height: {(h - 2 * margin - 1.2) / 6 - 0.25:.2f}in; }}
</style></head><body>{body}</body></html>"""


async def html_to_pdf(html_text: str, out: Path, w: float, h: float) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with plain_browser() as browser:
        page = await browser.new_page()
        await page.set_content(html_text, wait_until="load")
        await page.pdf(path=str(out), width=f"{w}in", height=f"{h}in", print_background=True,
                       margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    return out


def _rasterised_page(pdf: Path, index: int, dpi: int = 600):
    """Turn one page into a pure black/white 600 DPI image page (removes font problems)."""
    img = pdftools.render(pdf, index, dpi).convert("L").point(lambda v: 0 if v < 128 else 255, "1")
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=dpi)
    buf.seek(0)
    return PdfReader(buf).pages[0]


def _ink_box_points(pic: Picture) -> tuple[float, float, float, float]:
    if pic.bbox_in is None:
        info = pdftools.check(pic.pdf, deep=False)
        pic.bbox_in = info.page_info[pic.index].bbox_in
    x0, y0, x1, y1 = pic.bbox_in
    pad = 0.04
    return ((x0 - pad) * 72, (y0 - pad) * 72, (x1 + pad) * 72, (y1 + pad) * 72)


async def build_interior(title: str, subtitle: str, pictures: list[Picture], trim: str,
                         out: Path, pen_name: str | None = None) -> dict:
    """Build the full interior. Returns {"path", "pages", "trim", "picture_pages"}."""
    pen_name = pen_name or settings.load_config()["agent"]["pen_name"]
    w, h = specs.parse_trim(trim)
    total = 4 + 2 * len(pictures) + 2
    margin = safe_margin(total)
    work = out.parent / "_work"
    front = await html_to_pdf(front_matter_html(title, subtitle, pen_name, trim, margin), work / "front.pdf", w, h)
    back = await html_to_pdf(test_page_html(trim, margin), work / "test_page.pdf", w, h)

    writer = PdfWriter()
    for p in PdfReader(str(front)).pages:
        writer.add_page(p)
    if len(writer.pages) != 4:
        raise RuntimeError(f"Front matter came out as {len(writer.pages)} pages, expected 4")

    box_w, box_h = (w - 2 * margin) * 72, (h - 2 * margin) * 72
    picture_pages = []
    readers: dict[Path, PdfReader] = {}
    for pic in pictures:
        if pic.rasterise:
            src = _rasterised_page(pic.pdf, pic.index)
        else:
            reader = readers.setdefault(pic.pdf, PdfReader(str(pic.pdf)))
            src = reader.pages[pic.index]
        bx0, by0, bx1, by1 = _ink_box_points(pic)
        # Content coordinates start at the page's crop box corner.
        cx, cy = float(src.cropbox.left), float(src.cropbox.bottom)
        bw, bh = bx1 - bx0, by1 - by0
        scale = min(1.0, box_w / bw, box_h / bh)
        dx = margin * 72 + (box_w - bw * scale) / 2
        dy = margin * 72 + (box_h - bh * scale) / 2
        target = writer.add_blank_page(w * 72, h * 72)
        t = Transformation().translate(-(cx + bx0), -(cy + by0)).scale(scale, scale).translate(dx, dy)
        target.merge_transformed_page(src, t)
        if "/Annots" in target:
            del target["/Annots"]
        picture_pages.append(len(writer.pages))           # 1-based page number
        writer.add_blank_page(w * 72, h * 72)              # blank back
    for p in PdfReader(str(back)).pages:
        writer.add_page(p)

    for page in writer.pages:
        page.mediabox.lower_left = (0, 0)
        page.mediabox.upper_right = (w * 72, h * 72)
        page.cropbox = page.mediabox
        if "/Annots" in page:
            del page["/Annots"]
    writer.metadata = None
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        writer.write(f)
    pages = len(writer.pages)
    log.info("Built %s: %d pages at %s", out, pages, trim)
    return {"path": str(out), "pages": pages, "trim": trim, "picture_pages": picture_pages,
            "margin_in": margin}


def pictures_from_report(rep: pdftools.Report) -> list[Picture]:
    raster = bool(rep.fonts_not_embedded)
    return [Picture(Path(rep.file), p.index, p.bbox_in, rasterise=raster and bool(p.text))
            for p in rep.pictures]


def save_png_preview(pdf: Path, page_number: int, out: Path, dpi: int = 110) -> Path:
    img = pdftools.render(pdf, page_number - 1, dpi, smooth=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def contact_sheet(images: list[Path], out: Path, cols: int = 5, cell: int = 300) -> Path:
    """All pages small in one image, numbered (for Telegram)."""
    from PIL import ImageDraw
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 30)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(images):
        im = Image.open(path).convert("RGB")
        im.thumbnail((cell - 16, cell - 16))
        x, y = (i % cols) * cell, (i // cols) * (cell + 30)
        sheet.paste(im, (x + (cell - im.width) // 2, y + 26 + (cell - 16 - im.height) // 2))
        draw.rectangle([x + 4, y + 22, x + cell - 4, y + cell + 26], outline=(180, 180, 180))
        draw.text((x + 10, y + 4), str(i + 1), fill=(200, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out

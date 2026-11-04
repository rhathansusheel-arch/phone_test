"""PDF checker: looks at an interior PDF the way KDP's reviewers would.

Checks: trim size and page count, margins (no bleed), 300 DPI images, fonts embedded,
no bookmarks/comments/metadata, pure black on white (no grey/colour), lines at least
0.75 pt, one picture per page with blank backs.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image
from pypdf import PdfReader

from . import specs

FRONT_MATTER_WORDS = re.compile(
    r"belongs to|copyright|all rights reserved|©|test (your )?colou?rs?|colou?r test|"
    r"isbn|published by|thank you|this book is|printed in", re.I)


@dataclass
class PageInfo:
    index: int                    # 0-based
    kind: str                     # blank / picture / text
    ink: float                    # share of the page that is dark
    bbox_in: tuple | None         # (left, bottom, right, top) of the ink, inches from bottom-left
    text: str = ""
    grey: float = 0.0             # share of page in real grey areas (not just line edges)
    colour: float = 0.0           # share of page in colour
    thin: float = 0.0             # share of dark pixels in lines thinner than 0.75 pt
    min_dpi: float | None = None  # lowest image resolution on the page


@dataclass
class Report:
    file: str
    pages: int
    size_in: tuple
    trim: str | None
    page_info: list[PageInfo]
    issues: list[dict] = field(default_factory=list)   # {"level": fix|you|info, "text": ...}
    fonts_not_embedded: list[str] = field(default_factory=list)
    has_bookmarks: bool = False
    has_comments: bool = False
    metadata: dict = field(default_factory=dict)

    def add(self, level: str, text: str) -> None:
        self.issues.append({"level": level, "text": text})

    @property
    def pictures(self) -> list[PageInfo]:
        return [p for p in self.page_info if p.kind == "picture"]

    def needs_you(self) -> list[str]:
        return [i["text"] for i in self.issues if i["level"] == "you"]

    def text_summary(self) -> str:
        icon = {"fix": "🔧", "you": "❗", "info": "ℹ️", "ok": "✅"}
        lines = [f"{Path(self.file).name}: {self.pages} pages, {self.size_in[0]:.2f} x {self.size_in[1]:.2f} in, "
                 f"{len(self.pictures)} pictures"]
        lines += [f"{icon[i['level']]} {i['text']}" for i in self.issues]
        return "\n".join(lines)


def render(pdf_path: str | Path, index: int, dpi: int, smooth: bool = False) -> Image.Image:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[index]
        bitmap = page.render(scale=dpi / 72, no_smoothtext=not smooth, no_smoothimage=not smooth,
                             no_smoothpath=not smooth, fill_color=(255, 255, 255, 255))
        return bitmap.to_pil().convert("RGB")
    finally:
        pdf.close()


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def _runs(mask: np.ndarray) -> np.ndarray:
    """For each True pixel, the length of the horizontal run of True pixels it belongs to."""
    h, w = mask.shape
    padded = np.zeros((h, w + 1), dtype=bool)
    padded[:, :w] = mask
    flat = padded.ravel()
    d = np.diff(np.concatenate(([0], flat.astype(np.int8), [0])))
    lengths = np.flatnonzero(d == -1) - np.flatnonzero(d == 1)
    out = np.zeros(flat.shape, dtype=np.int32)
    out[flat] = np.repeat(lengths, lengths)
    return out.reshape(h, w + 1)[:, :w]


def analyse_image(img: Image.Image, dpi: int) -> dict:
    a = np.asarray(img, dtype=np.int16)
    grey_level = a.mean(axis=2)
    spread = a.max(axis=2) - a.min(axis=2)
    dark = grey_level < 110
    colour = spread > 40
    mid = (grey_level >= 110) & (grey_level < 225) & ~colour
    near_dark = _dilate(dark, 2)
    real_grey = mid & ~near_dark
    total = a.shape[0] * a.shape[1]
    ink_mask = grey_level < 225
    ys, xs = np.nonzero(ink_mask)
    bbox_px = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) if len(xs) else None
    return {"ink": float(dark.sum()) / total, "grey": float(real_grey.sum()) / total,
            "colour": float((colour & ink_mask).sum()) / total, "bbox_px": bbox_px, "dark": dark}


def thin_share(dark: np.ndarray, dpi: int, min_pt: float) -> float:
    """Share of dark pixels in strokes thinner than min_pt (checks both directions)."""
    if dark.sum() == 0:
        return 0.0
    limit = min_pt / 72 * dpi - 0.5
    horiz = _runs(dark)
    vert = _runs(dark.T).T
    thickness = np.minimum(horiz, vert)
    return float(((thickness < limit) & dark).sum()) / float(dark.sum())


def _image_dpis(pdf_path: Path, index: int) -> list[float]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[index]
        dpis = []
        for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3):
            left, bottom, right, top = obj.get_pos()
            w_in, h_in = (right - left) / 72, (top - bottom) / 72
            try:
                px_w, px_h = obj.get_px_size()
            except Exception:
                continue
            if w_in > 0.2 and h_in > 0.2:
                dpis.append(min(px_w / w_in, px_h / h_in))
        return dpis
    finally:
        pdf.close()


def _fonts_not_embedded(reader: PdfReader) -> list[str]:
    missing = set()
    for page in reader.pages:
        res = page.get("/Resources")
        fonts = res.get_object().get("/Font") if res else None
        if not fonts:
            continue
        for _, ref in fonts.get_object().items():
            font = ref.get_object()
            if font.get("/Subtype") == "/Type3":
                continue
            target = font
            if font.get("/Subtype") == "/Type0":
                target = font["/DescendantFonts"][0].get_object()
            desc = target.get("/FontDescriptor")
            desc = desc.get_object() if desc else None
            if not desc or not any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                missing.add(str(font.get("/BaseFont", "unknown")))
    return sorted(missing)


def check(pdf_path: str | Path, fmt: str = "paperback", deep: bool = True,
          deep_dpi: int = 300, scan_dpi: int = 100, final: bool = False) -> Report:
    """Check one interior PDF. deep=True also checks greys and line thickness at 300 DPI.
    final=True checks a finished interior (built by the agent) instead of planning fixes."""
    pdf_path = Path(pdf_path)
    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    sizes = Counter((round(float(p.mediabox.width) / 72, 2), round(float(p.mediabox.height) / 72, 2))
                    for p in reader.pages)
    size = sizes.most_common(1)[0][0]
    allowed = specs.paperback_trims() if fmt == "paperback" else {t: 0 for t in specs.hardcover_trims()}
    trim = next((t for t in allowed
                 if abs(specs.parse_trim(t)[0] - size[0]) < 0.02 and abs(specs.parse_trim(t)[1] - size[1]) < 0.02),
                None)
    infos: list[PageInfo] = []
    for i in range(n):
        img = render(pdf_path, i, scan_dpi)
        a = analyse_image(img, scan_dpi)
        try:
            text = (reader.pages[i].extract_text() or "").strip()
        except Exception:
            text = ""
        bbox_in = None
        if a["bbox_px"]:
            x0, y0, x1, y1 = a["bbox_px"]
            h_px = img.height
            bbox_in = (float(x0) / scan_dpi, float(h_px - y1) / scan_dpi, float(x1) / scan_dpi, float(h_px - y0) / scan_dpi)
        if a["ink"] < 0.0005 and a["grey"] < 0.0005:
            kind = "blank"
        elif text and (FRONT_MATTER_WORDS.search(text) or (a["ink"] < 0.03 and len(text) > 15)):
            kind = "text"
        elif text and i < 2 and a["ink"] < 0.06:
            kind = "text"   # title page
        else:
            kind = "picture"
        info = PageInfo(i, kind, a["ink"], bbox_in, text[:120], a["grey"], a["colour"])
        dpis = _image_dpis(pdf_path, i)
        info.min_dpi = min(dpis) if dpis else None
        if deep and kind == "picture":
            big = render(pdf_path, i, deep_dpi)
            b = analyse_image(big, deep_dpi)
            info.grey, info.colour = b["grey"], b["colour"]
            info.thin = thin_share(b["dark"], deep_dpi, float(specs.get()["paperback"]["min_line_pt"]))
        infos.append(info)

    rep = Report(str(pdf_path), n, size, trim, infos)
    rep.fonts_not_embedded = _fonts_not_embedded(reader)
    rep.has_bookmarks = bool(reader.outline)
    rep.has_comments = any("/Annots" in p for p in reader.pages)
    meta = reader.metadata or {}
    rep.metadata = {k: str(v) for k, v in meta.items()}
    if final:
        _judge_final(rep, fmt)
    else:
        _judge(rep, fmt)
    return rep


def _judge(rep: Report, fmt: str) -> None:
    s = specs.get()["paperback"]
    if rep.trim is None:
        rep.add("fix", f"Page size {rep.size_in[0]:.2f} x {rep.size_in[1]:.2f} in is not a KDP {fmt} size "
                       f"- I'll resize to the nearest one.")
    pics = rep.pictures
    if not pics:
        rep.add("you", "I couldn't find any pictures in this PDF.")
        return
    # Blank backs
    no_back = [p.index + 1 for p in pics
               if p.index + 1 < rep.pages and rep.page_info[p.index + 1].kind != "blank"]
    if pics and (no_back or pics[-1].index + 1 >= rep.pages):
        rep.add("fix", f"{len(no_back)} pictures have no blank back (markers would bleed through) - I'll add them.")
    # Front matter
    texts = [p for p in rep.page_info if p.kind == "text"]
    if texts:
        rep.add("info", f"Found {len(texts)} text page(s) (title/copyright etc.) on page(s) "
                        f"{', '.join(str(p.index + 1) for p in texts)} - I'll replace them with fresh front matter.")
    rep.add("fix", "I'll add: title page, 'This book belongs to' page, copyright page and a colour test page.")
    # Margins (on the source size; fixed by fitting every picture inside safe margins)
    w, h = rep.size_in
    out = s["outside_margin_no_bleed"]
    final_pages = 4 + 2 * len(pics) + 2
    gut = specs.gutter(final_pages)
    tight = [p.index + 1 for p in pics if p.bbox_in and (
        p.bbox_in[0] < gut or p.bbox_in[1] < out or p.bbox_in[2] > w - gut or p.bbox_in[3] > h - out)]
    if tight:
        rep.add("fix", f"{len(tight)} picture(s) reach into the margins (pages {_short(tight)}) - "
                       f"I'll shrink them slightly to fit.")
    # Image resolution
    low = [(p.index + 1, p.min_dpi) for p in pics if p.min_dpi and p.min_dpi < s["min_dpi"] - 1]
    if low:
        rep.add("you", f"{len(low)} picture(s) are below {s['min_dpi']} DPI (lowest {min(d for _, d in low):.0f} DPI, "
                       f"pages {_short([i for i, _ in low])}). They may print blurry.")
    # Colour and grey
    col = [p.index + 1 for p in pics if p.colour > 0.001]
    if col:
        rep.add("you", f"Colour found on pages {_short(col)}. Colouring pages must be black on white.")
    grey = [p.index + 1 for p in pics if p.grey > 0.003]
    if grey:
        rep.add("you", f"Grey areas/shading found on pages {_short(grey)}. Pages should be pure black on white.")
    thin = [p.index + 1 for p in pics if p.thin > 0.25]
    if thin:
        rep.add("you", f"Very thin lines (under {s['min_line_pt']} pt) on pages {_short(thin)} - they may not print.")
    # Fonts, bookmarks, comments, metadata
    if rep.fonts_not_embedded:
        rep.add("fix", f"Fonts not embedded ({', '.join(rep.fonts_not_embedded[:3])}) - "
                       f"pages using them will be turned into high-resolution images.")
    if rep.has_bookmarks:
        rep.add("fix", "Bookmarks found - I'll remove them.")
    if rep.has_comments:
        rep.add("fix", "Comments/annotations found - I'll remove them.")
    if rep.metadata:
        rep.add("fix", "Metadata found - I'll remove it.")
    # Too similar pages (exact duplicates)
    if len(pics) < 12:
        rep.add("info", f"Only {len(pics)} pictures - a short book.")


def _judge_final(rep: Report, fmt: str) -> None:
    """Rules for a finished interior. Anything wrong here is a 'you' problem (the agent can't fix it twice)."""
    s = specs.get()["paperback"]
    problem = specs.page_count_ok(fmt, rep.trim or "?", rep.pages) if rep.trim else \
        f"page size {rep.size_in[0]:.2f} x {rep.size_in[1]:.2f} in is not a KDP {fmt} size"
    if problem:
        rep.add("you", problem)
    else:
        rep.add("ok", f"{rep.trim} in, {rep.pages} pages - allowed for {fmt}")
    pics = rep.pictures
    wrong_side = [p.index + 1 for p in pics if (p.index + 1) % 2 == 0]
    no_back = [p.index + 1 for p in pics if p.index + 1 >= rep.pages or rep.page_info[p.index + 1].kind != "blank"]
    if wrong_side or no_back:
        rep.add("you", f"Picture layout wrong: left-hand pictures {_short(wrong_side)}, no blank back {_short(no_back)}")
    else:
        rep.add("ok", f"{len(pics)} pictures, each on a right-hand page with a blank back")
    w, h = rep.size_in
    out, gut = s["outside_margin_no_bleed"], specs.gutter(rep.pages)
    bad = []
    for p in rep.page_info:
        if not p.bbox_in:
            continue
        right_hand = (p.index + 1) % 2 == 1
        left_m, right_m = (gut, out) if right_hand else (out, gut)
        x0, y0, x1, y1 = p.bbox_in
        if x0 < left_m - 0.01 or x1 > w - right_m + 0.01 or y0 < out - 0.01 or y1 > h - out + 0.01:
            bad.append(p.index + 1)
    if bad:
        rep.add("you", f"Content inside the margins on pages {_short(bad)}")
    else:
        rep.add("ok", f"Margins clear (outside {out} in, gutter {gut} in)")
    low = [p.index + 1 for p in pics if p.min_dpi and p.min_dpi < s["min_dpi"] - 1]
    col = [p.index + 1 for p in pics if p.colour > 0.001]
    grey = [p.index + 1 for p in pics if p.grey > 0.003]
    thin = [p.index + 1 for p in pics if p.thin > 0.25]
    for nums, ok_text, bad_text in [
            (low, f"Images {s['min_dpi']} DPI or more", "Low-resolution images on pages"),
            (col, "No colour", "Colour on pages"),
            (grey, "No grey shading", "Grey shading on pages"),
            (thin, f"Lines at least {s['min_line_pt']} pt", "Very thin lines on pages")]:
        rep.add("you", f"{bad_text} {_short(nums)}") if nums else rep.add("ok", ok_text)
    clean = not (rep.fonts_not_embedded or rep.has_bookmarks or rep.has_comments or rep.metadata)
    rep.add("ok" if clean else "you", "Fonts embedded, no bookmarks, comments or metadata" if clean else
            f"Leftovers: fonts={rep.fonts_not_embedded} bookmarks={rep.has_bookmarks} "
            f"comments={rep.has_comments} metadata={list(rep.metadata)}")


def _short(nums: list[int]) -> str:
    return ", ".join(map(str, nums[:8])) + (" ..." if len(nums) > 8 else "")


def crop_picture_png(pdf_path: str | Path, index: int, dpi: int, out: Path, pad_in: float = 0.15) -> Path:
    """Save one picture, cropped to its drawing, as a PNG (for covers and previews)."""
    img = render(pdf_path, index, dpi, smooth=True)
    a = analyse_image(img, dpi)
    if a["bbox_px"]:
        x0, y0, x1, y1 = a["bbox_px"]
        pad = int(pad_in * dpi)
        img = img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, dpi=(dpi, dpi))
    return out

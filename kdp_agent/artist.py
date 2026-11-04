"""Colouring page artist: Claude draws each page as SVG line art, then every page is checked.

books/<slug>/pages.json keeps every page's state, saved after each page, so a usage limit
or restart loses nothing. Drawing is spread out: a few pages per run.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from . import interior, jobs, pdftools, settings, specs, taste
from .browser import plain_browser
from .claude_cli import ask_claude, extract_json, extract_svg
from .safety import find_trademarks

log = logging.getLogger("artist")

FORBIDDEN_TAGS = {"text", "tspan", "textpath", "image", "lineargradient", "radialgradient", "filter", "pattern",
                  "mask", "foreignobject", "script", "style", "use", "symbol", "clippath"}
OK_COLOURS = {"none", "white", "#fff", "#ffffff", "black", "#000", "#000000", "transparent"}
MIN_LINE_PT = {"toddler": 2.0, "kids": 1.2, "adult": 0.75}
PAGES_PER_RUN = 6


def style_for(plan: dict) -> str:
    ages = plan.get("ages")
    if ages and ages[1] <= 5:
        return "toddler"
    if ages:
        return "kids"
    return "adult"


def pages_file(slug: str) -> Path:
    return jobs.book_dir(slug) / "pages.json"


def load_pages(slug: str) -> dict:
    return settings.read_json(pages_file(slug), {"pages": [], "approved": False})


def save_pages(slug: str, data: dict) -> None:
    settings.write_json(pages_file(slug), data)


def canvas(plan: dict) -> tuple[int, int, float, float]:
    """SVG canvas size (units) and the drawing box (inches) for this book's trim."""
    w, h = specs.parse_trim(plan.get("trim") or settings.load_config()["books"]["default_trim"])
    m = 0.5
    bw, bh = w - 2 * m, h - 2 * m
    return 1000, round(1000 * bh / bw), bw, bh


# ------------------------------------------------------------ 1. book plan + page list

async def plan_pages(slug: str) -> dict:
    """Write plan.json details (copied from top sellers) and a page list with no repeats."""
    from .pipeline import load_plan, save_plan
    plan = load_plan(slug)
    summary = plan.get("research_summary") or {}
    if not plan.get("trim"):
        common = summary.get("common_trim")
        plan["trim"] = common if common in specs.paperback_trims() else settings.load_config()["books"]["default_trim"]
    if not plan.get("picture_count"):
        pages = summary.get("median_pages")
        guess = round((pages - 6) / 2) if pages else (40 if plan.get("ages") else 30)
        plan["picture_count"] = max(20, min(50, guess))
    style = style_for(plan)
    plan.setdefault("style_guide", f"style_{style}.md")
    plan.setdefault("line_thickness", {"toddler": "very thick", "kids": "bold", "adult": "medium-bold"}[style])
    plan.setdefault("detail_level", {"toddler": "very simple", "kids": "simple with some details",
                                     "adult": "bold and easy, moderate detail"}[style])
    plan.setdefault("cover_finish", settings.load_config()["books"]["cover_finish"])
    plan.setdefault("formats", ["paperback", "hardcover"])
    data = load_pages(slug)
    if not data["pages"]:
        others = _other_subjects(slug)
        n = plan["picture_count"]
        answer = await ask_claude(
            f"Plan the pictures for a colouring book.\nTheme: {plan['theme']}\nAudience: {plan['audience']}"
            f" (ages {plan.get('ages') or 'adult'})\nNumber of pictures: {n}\nStyle: {plan['detail_level']}\n"
            f"Owner's taste notes: {'; '.join(taste.reasons(last=10)) or 'none'}\n"
            f"Subjects already used in the owner's other books (avoid repeating these): {', '.join(others[:150]) or 'none'}\n\n"
            "Rules: every picture a DIFFERENT subject; no trademarks, brands, logos or characters from films/TV/"
            "games/books; nothing scary; each fits on one page as a single main subject with a simple background.\n"
            f"Return ONLY a JSON array of {n} objects: "
            '[{"subject": "smiling whale", "details": "one line describing pose and a simple background"}]')
        items = extract_json(answer)
        seen, pages = set(), []
        for it in items:
            subj = str(it.get("subject", "")).strip()
            if not subj or subj.lower() in seen or find_trademarks(subj, it.get("details", "")):
                continue
            seen.add(subj.lower())
            pages.append({"n": len(pages) + 1, "subject": subj, "details": it.get("details", ""),
                          "status": "todo", "tries": 0, "problems": [], "feedback": None})
        data["pages"] = pages[:n]
        save_pages(slug, data)
    plan["subjects"] = [p["subject"] for p in data["pages"]]
    save_plan(slug, plan)
    return plan


def _other_subjects(slug: str) -> list[str]:
    out = []
    for d in settings.BOOKS.glob("*/pages.json"):
        if d.parent.name != slug:
            out += [p["subject"] for p in settings.read_json(d, {}).get("pages", [])]
    for d in settings.BOOKS.glob("*/plan.json"):
        if d.parent.name != slug:
            out += settings.read_json(d, {}).get("subjects", [])
    return out


# ------------------------------------------------------------ 2. drawing one page

DRAW_PROMPT = """{style}

CANVAS: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"> ... </svg>
(the whole canvas is the printable area; it will be printed {bw:.1f} x {bh:.1f} inches)

PAGE {n} of a colouring book about "{theme}" for {audience}:
Subject: {subject}
Details: {details}
{feedback}
Reply with ONLY the complete SVG code, nothing else."""


def static_problems(svg: str) -> list[str]:
    """Check the SVG code itself (before drawing it)."""
    problems = []
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return [f"The SVG is not valid XML ({exc})."]
    if not root.tag.lower().endswith("svg"):
        return ["The answer is not an <svg> element."]
    bad_tags, bad_colours, see_through = set(), set(), False
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag in FORBIDDEN_TAGS:
            bad_tags.add(tag)
        props = dict(el.attrib)
        for part in props.get("style", "").split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                props[k.strip()] = v.strip()
        for key in ("fill", "stroke", "stop-color", "color"):
            v = props.get(key)
            if v is not None and v.strip().lower() not in OK_COLOURS:
                bad_colours.add(v)
        for key in ("opacity", "fill-opacity", "stroke-opacity"):
            try:
                if key in props and float(props[key]) < 1:
                    see_through = True
            except ValueError:
                see_through = True
    if bad_tags:
        problems.append(f"Remove these elements: {', '.join(sorted(bad_tags))} (no text, images, gradients, filters).")
    if bad_colours:
        problems.append(f"Only black lines and white/none fills are allowed - found colours: {', '.join(sorted(bad_colours))[:120]}.")
    if see_through:
        problems.append("No transparency/opacity - everything must be solid black or white.")
    return problems


async def render_svg(svg: str, out: Path, width_px: int, height_px: int, crisp: bool) -> Path:
    css = "svg * { shape-rendering: crispEdges !important; }" if crisp else ""
    page_html = (f"<html><head><style>html,body{{margin:0;background:#fff}} svg{{display:block;width:{width_px}px;"
                 f"height:{height_px}px}} {css}</style></head><body>{svg}</body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    async with plain_browser() as browser:
        page = await browser.new_page(viewport={"width": width_px, "height": height_px})
        await page.set_content(page_html)
        await page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": width_px, "height": height_px})
    return out


def dhash(img: Image.Image) -> int:
    small = np.asarray(img.convert("L").resize((17, 16), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def known_hashes(slug: str, skip_n: int | None = None) -> dict[str, int]:
    out = {}
    for f in settings.BOOKS.glob("*/pages.json"):
        for p in settings.read_json(f, {}).get("pages", []):
            if p.get("hash") is None or (f.parent.name == slug and p["n"] == skip_n):
                continue
            out[f"{f.parent.name} page {p['n']}"] = int(p["hash"])
    return out


def pixel_problems(img: Image.Image, dpi: int, style: str, slug: str, n: int) -> tuple[list[str], dict]:
    cfg = settings.load_config()["artist"]
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    dark = (a < 60).all(axis=2)
    white = (a > 200).all(axis=2)
    other = ~(dark | white)
    total = a.shape[0] * a.shape[1]
    problems = []
    if other.sum() / total > 0.0005:
        problems.append("Grey or colour pixels found - use only black strokes and white.")
    ink = dark.sum() / total
    if ink < cfg["min_ink"]:
        problems.append(f"Too little drawing ({ink:.1%} black) - make the subject bigger and lines bolder.")
    if ink > cfg["max_ink"]:
        problems.append(f"Too much black ({ink:.1%}) - leave more white space to colour, fewer solid areas.")
    ys, xs = np.nonzero(dark)
    info = {"ink": float(ink)}
    if len(xs):
        h, w = dark.shape
        edge = int(min(w, h) * 0.02)
        if xs.min() < edge or ys.min() < edge or xs.max() > w - edge or ys.max() > h - edge:
            problems.append("The drawing touches the edge - keep a clear border of at least 60 units on every side.")
        cx, cy = (xs.min() + xs.max()) / 2 / w, (ys.min() + ys.max()) / 2 / h
        if abs(cx - 0.5) > 0.12 or abs(cy - 0.5) > 0.12:
            problems.append("The picture is not centred - centre the main subject.")
        fill_w, fill_h = (xs.max() - xs.min()) / w, (ys.max() - ys.min()) / h
        if max(fill_w, fill_h) < 0.5:
            problems.append("The subject is too small - make it fill most of the canvas.")
        thin = pdftools.thin_share(dark, dpi, MIN_LINE_PT[style])
        info["thin"] = thin
        if thin > 0.25:
            problems.append(f"Lines are too thin for this age group - use thicker strokes.")
    h_val = dhash(img)
    info["hash"] = h_val
    limit = cfg["similarity_limit"]
    for name, other_hash in known_hashes(slug, skip_n=n).items():
        if hamming(h_val, other_hash) < limit:
            problems.append(f"Too similar to {name} - draw it differently (pose, angle, background).")
            break
    return problems, info


async def draw_page(slug: str, page: dict, plan: dict) -> dict:
    """Draw one page, check it, retry up to max_retries times with notes. Saves files in pages/."""
    style = style_for(plan)
    guide = (settings.PROMPTS / plan.get("style_guide", f"style_{style}.md")).read_text(encoding="utf-8")
    w_units, h_units, bw, bh = canvas(plan)
    folder = jobs.book_dir(slug) / "pages"
    retries = settings.load_config()["artist"]["max_retries"]
    problems: list[str] = []
    for attempt in range(retries + 1):
        notes = ""
        if page.get("feedback"):
            notes += f"\nTHE OWNER ASKED FOR THIS CHANGE: {page['feedback']}\n"
        if problems:
            notes += "\nYOUR LAST DRAWING FAILED THESE CHECKS - fix them:\n- " + "\n- ".join(problems) + "\n"
        answer = await ask_claude(DRAW_PROMPT.format(
            style=guide, w=w_units, h=h_units, bw=bw, bh=bh, n=page["n"], theme=plan["theme"],
            audience=plan["audience"], subject=page["subject"], details=page.get("details", ""), feedback=notes))
        page["tries"] = page.get("tries", 0) + 1
        try:
            svg = extract_svg(answer)
        except ValueError:
            problems = ["Reply with SVG code only."]
            continue
        svg = re.sub(r"<svg\b", '<svg preserveAspectRatio="xMidYMid meet"', svg, count=1) \
            if "preserveAspectRatio" not in svg else svg
        problems = static_problems(svg)
        if problems:
            continue
        svg_path = folder / f"{page['n']:02d}.svg"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text(svg, encoding="utf-8")
        dpi = 300
        crisp = await render_svg(svg, folder / f"{page['n']:02d}_check.png", int(bw * dpi), int(bh * dpi), crisp=True)
        img = Image.open(crisp)
        problems, info = await asyncio.to_thread(pixel_problems, img, dpi, style, slug, page["n"])
        crisp.unlink(missing_ok=True)
        if not problems:
            await render_svg(svg, folder / f"{page['n']:02d}.png", int(bw * 60), int(bh * 60), crisp=False)
            page.update(status="done", problems=[], hash=str(info["hash"]), ink=info["ink"], feedback=None)
            return page
    page.update(status="flagged", problems=problems)
    return page


# ------------------------------------------------------------ 3. drawing a book + your review

async def draw_and_review(slug: str) -> bool:
    """Draw a few pages per run. When all are drawn, send the contact sheet. True when approved."""
    from .pipeline import load_plan
    from .telegram_bot import ask_me, send_photos
    plan = load_plan(slug)
    data = load_pages(slug)
    if data.get("approved"):
        return True
    todo = [p for p in data["pages"] if p["status"] == "todo"]
    for page in todo[:PAGES_PER_RUN]:
        log.info("Drawing %s page %d: %s", slug, page["n"], page["subject"])
        jobs.update_book(slug, doing=f"drawing page {page['n']} of {len(data['pages'])}")
        await draw_page(slug, page, plan)
        save_pages(slug, data)                      # save after every page
    if any(p["status"] == "todo" for p in data["pages"]):
        jobs.snooze(slug, 10 * 60)                  # spread the work out
        return False
    images = [jobs.book_dir(slug) / "pages" / f"{p['n']:02d}.png" for p in data["pages"]]
    images = [i if i.exists() else _placeholder(i) for i in images]
    sheet = interior.contact_sheet(images, jobs.book_dir(slug) / "previews" / "pages_contact_sheet.png")
    flagged = [p for p in data["pages"] if p["status"] == "flagged"]
    msg = f"🎨 All {len(data['pages'])} pages for '{plan['theme']}' are drawn (numbers on the sheet)."
    if flagged:
        msg += "\n❗ These failed my checks after retries: " + "; ".join(
            f"{p['n']} ({p['subject']}): {p['problems'][0] if p['problems'] else ''}" for p in flagged)
    answer = await ask_me(msg + "\nApprove all, or tell me which pages to redo and why (e.g. '3, 7 too busy').",
                          buttons=["Approve all", "Redo pages"], typed_for=["Redo pages"], photos=[sheet],
                          about=f"pages:{slug}")
    if not answer:
        return False
    if answer["choice"] == "Approve all" and not flagged:
        taste.record("pages", "approved", plan["theme"])
        await compile_pages(slug)
        data["approved"] = True
        save_pages(slug, data)
        return True
    text = answer.get("text") or ""
    nums = {int(x) for x in re.findall(r"\d+", text)} if text else set()
    if answer["choice"] == "Approve all":
        nums = {p["n"] for p in flagged}
        text = "failed the automatic checks"
    for p in data["pages"]:
        if p["n"] in nums:
            p.update(status="todo", feedback=text, tries=0)
            taste.record("pages", "rejected", p["subject"], reason=text)
    save_pages(slug, data)
    return False


def _placeholder(path: Path) -> Path:
    img = Image.new("RGB", (450, 600), "white")
    out = path.with_name(path.stem + "_missing.png")
    img.save(out)
    return out


async def compile_pages(slug: str) -> Path:
    """All approved SVGs -> pages.pdf (one vector page each), for the interior builder."""
    from .pipeline import load_plan
    plan = load_plan(slug)
    _, _, bw, bh = canvas(plan)
    data = load_pages(slug)
    body = "".join(
        f'<section style="width:{bw}in;height:{bh}in;page-break-after:always;overflow:hidden">'
        + (jobs.book_dir(slug) / "pages" / f"{p['n']:02d}.svg").read_text(encoding="utf-8")
          .replace("<svg", f'<svg style="width:{bw}in;height:{bh}in;display:block"', 1)
        + "</section>" for p in data["pages"])
    html_text = (f"<html><head><style>@page{{size:{bw}in {bh}in;margin:0}} body{{margin:0}}</style></head>"
                 f"<body>{body}</body></html>")
    out = jobs.book_dir(slug) / "pages.pdf"
    await interior.html_to_pdf(html_text, out, bw, bh)
    rep = await asyncio.to_thread(pdftools.check, out, "paperback", False)
    settings.write_json(jobs.book_dir(slug) / "pages_bbox.json", [p.bbox_in for p in rep.page_info])
    return out


def picture_sources(slug: str) -> list[interior.Picture]:
    pdf = jobs.book_dir(slug) / "pages.pdf"
    boxes = settings.read_json(jobs.book_dir(slug) / "pages_bbox.json", [])
    return [interior.Picture(pdf, i, tuple(b) if b else None) for i, b in enumerate(boxes)]

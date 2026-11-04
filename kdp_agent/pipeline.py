"""The book pipeline: what happens at each stage.

planned  -> (inbox book: check the PDF)            -> drawing
drawing  -> (new book: draw pages; inbox: nothing) -> build interiors, listing, covers -> built
built    -> send you the cover, samples, listing; Approve -> listed / Change -> rebuild
(listed onwards: see kdp_upload.py)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import shutil
from pathlib import Path

from . import covers, decisions, interior, jobs, listing, pdftools, research, settings, specs, taste
from .claude_cli import ask_claude, extract_json
from .errors import NeedsHuman, StopBook
from .jobs import stage_worker

log = logging.getLogger("pipeline")

# The three finished PDFs (CLAUDE.md "First jobs").
FIRST_JOBS = {
    "ocean": {"label": "Ocean & sea-life (ages 2-5)", "theme": "Ocean & sea-life animals",
              "audience": "toddlers and preschool kids", "ages": [2, 5],
              "style": "big simple pictures with thick bold lines",
              "seed": "ocean coloring book for toddlers",
              "audience_words": ["toddlers", "kids ages 2-4", "kids ages 2-5", "preschool"],
              "words": ["ocean", "sea", "fish", "marine", "underwater"]},
    "vehicles": {"label": "Vehicles (ages 4-8)", "theme": "Vehicles (cars, trucks, diggers, planes, trains, boats)",
                 "audience": "kids", "ages": [4, 8], "style": "clear pictures with bold lines and some detail",
                 "seed": "vehicles coloring book for kids",
                 "audience_words": ["kids ages 4-8", "boys", "kids ages 3-5", "toddlers"],
                 "words": ["vehicle", "car", "truck", "transport", "digger"]},
    "christmas": {"label": "Bold & Easy Christmas (adults, seniors)", "theme": "Bold & Easy Christmas",
                  "audience": "adults and seniors", "ages": None,
                  "style": "bold and easy designs with thick lines and large spaces",
                  "seed": "bold and easy christmas coloring book",
                  "audience_words": ["adults", "seniors", "women", "beginners"],
                  "words": ["christmas", "xmas", "holiday", "festive", "noel"]},
}


def book_path(slug: str, *parts: str) -> Path:
    return jobs.book_dir(slug).joinpath(*parts)


def load_plan(slug: str) -> dict:
    return settings.read_json(book_path(slug, "plan.json"), {})


def save_plan(slug: str, plan: dict) -> None:
    settings.write_json(book_path(slug, "plan.json"), plan)


def guess_job(filename: str) -> str | None:
    low = filename.lower()
    for key, job in FIRST_JOBS.items():
        if any(w in low for w in job["words"]):
            return key
    return None


# ------------------------------------------------------------ inbox intake

async def intake_inbox() -> list[str]:
    """Look for new PDFs in the inbox and ask you which book each one is. Returns new book slugs."""
    from .telegram_bot import ask_me
    seen_file = settings.DATA / "inbox_seen.json"
    seen = settings.read_json(seen_file, [])
    created = []
    for pdf in sorted(settings.INBOX.glob("*.pdf")):
        if pdf.name in seen:
            continue
        guess = guess_job(pdf.name)
        buttons = [FIRST_JOBS[k]["label"] for k in FIRST_JOBS] + ["Something else", "Ignore this file"]
        answer = await ask_me(
            f"📥 New PDF in the inbox: {pdf.name}\nWhich book is it?"
            + (f"\n(My guess: {FIRST_JOBS[guess]['label']})" if guess else ""),
            buttons=buttons, typed_for=["Something else"], about=f"inbox:{pdf.name}")
        if not answer or answer["choice"] == "Ignore this file":
            seen.append(pdf.name)
            settings.write_json(seen_file, seen)
            continue
        key = next((k for k in FIRST_JOBS if FIRST_JOBS[k]["label"] == answer["choice"]), None)
        if key:
            job = dict(FIRST_JOBS[key])
        else:
            job = {"label": answer.get("text") or pdf.stem, "theme": answer.get("text") or pdf.stem,
                   "audience": "kids", "ages": None, "style": "", "seed": f"{answer.get('text') or pdf.stem} coloring book",
                   "audience_words": []}
        manual = False
        if key == "christmas":
            a = await ask_me(
                "🎄 Christmas is time-sensitive. Will you upload it to KDP by hand this week, or should I do it?\n"
                "Either way I'll check the PDF and make the covers and listing for you.",
                buttons=["I'll upload it by hand", "You do it"], about="christmas-manual")
            manual = bool(a and a["choice"].startswith("I'll"))
        slug = jobs.make_slug(job["theme"])
        if jobs.load_book(slug):
            slug = jobs.make_slug(f"{job['theme']} {pdf.stem}")
        jobs.create_book(job["label"], slug=slug)
        book_path(slug).mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, book_path(slug, "source.pdf"))
        plan = {"source": "inbox", "inbox_file": pdf.name, "theme": job["theme"], "audience": job["audience"],
                "ages": job["ages"], "style": job["style"], "seed": job["seed"],
                "audience_words": job["audience_words"], "formats": ["paperback", "hardcover"],
                "cover_finish": settings.load_config()["books"]["cover_finish"], "manual_upload": manual}
        save_plan(slug, plan)
        seen.append(pdf.name)
        settings.write_json(seen_file, seen)
        created.append(slug)
        decisions.log({"type": "book_created", "slug": slug, "from": pdf.name, "manual": manual})
    return created


# ------------------------------------------------------------ stage: planned

def _report_to_json(rep: pdftools.Report) -> dict:
    return dataclasses.asdict(rep)


def _report_from_json(data: dict) -> pdftools.Report:
    pages = [pdftools.PageInfo(**p) for p in data.pop("page_info")]
    return pdftools.Report(page_info=pages, **data)


@stage_worker("planned")
async def plan_worker(book: dict) -> str | None:
    slug = book["slug"]
    plan = load_plan(slug)
    if plan.get("source") != "inbox":
        from . import artist
        await artist.plan_pages(slug)
        return "drawing"
    from .telegram_bot import ask_me, send_photos
    rep = await asyncio.to_thread(pdftools.check, book_path(slug, "source.pdf"))
    settings.write_json(book_path(slug, "check.json"), _report_to_json(rep))
    problems = rep.needs_you()
    if problems and not plan.get("accepted_check"):
        samples = []
        for i, p in enumerate(rep.pictures[:3]):
            samples.append(interior.save_png_preview(book_path(slug, "source.pdf"), p.index + 1,
                                                     book_path(slug, "previews", f"source_{i}.png")))
        await send_photos(samples)
        answer = await ask_me(f"🔍 I checked '{book['title']}':\n\n{rep.text_summary()}\n\n"
                              "The ❗ items are things I can't fix safely. Continue anyway, or stop so you can fix the PDF?",
                              buttons=["Continue anyway", "Stop (I'll fix the PDF)"], about=f"check:{slug}")
        if not answer or not answer["choice"].startswith("Continue"):
            taste.record("check", "rejected", book["title"], reason="owner will fix the PDF")
            raise StopBook("You chose to fix the PDF. Put the new PDF in the inbox and use /unhold.")
        plan["accepted_check"] = True
        save_plan(slug, plan)
    elif not problems:
        from .telegram_bot import notify
        await notify(f"🔍 Checked '{book['title']}':\n{rep.text_summary()}")
    plan["picture_count"] = len(rep.pictures)
    save_plan(slug, plan)
    return "drawing"


# ------------------------------------------------------------ stage: drawing

@stage_worker("drawing")
async def draw_worker(book: dict) -> str | None:
    slug = book["slug"]
    plan = load_plan(slug)
    if plan.get("source") != "inbox":
        from . import artist
        done = await artist.draw_and_review(slug)
        if not done:
            return None
    await build_book(slug)
    return "built"


def picture_sources(slug: str) -> list[interior.Picture]:
    plan = load_plan(slug)
    if plan.get("source") == "inbox":
        rep = _report_from_json(settings.read_json(book_path(slug, "check.json"), {}))
        rep.file = str(book_path(slug, "source.pdf"))
        return interior.pictures_from_report(rep)
    from . import artist
    return artist.picture_sources(slug)


async def find_subjects(slug: str, pictures: list[interior.Picture]) -> list[str]:
    """Ask Claude to look at the pictures and list what's in them (so the listing stays true)."""
    plan = load_plan(slug)
    if plan.get("subjects"):
        return plan["subjects"]
    previews = []
    for i, pic in enumerate(pictures):
        previews.append(pdftools.crop_picture_png(pic.pdf, pic.index, 40, book_path(slug, "previews", f"thumb_{i + 1:02d}.png")))
    sheet = interior.contact_sheet(previews, book_path(slug, "previews", "contact_sheet.png"), cols=8, cell=180)
    answer = await ask_claude(
        f"Look at the image file {sheet}. It is a contact sheet of the {len(pictures)} pictures in a colouring book, "
        "numbered. List what each picture shows in 1-4 words. Return ONLY a JSON array of strings, "
        "one per picture in order, e.g. [\"shark\", \"smiling octopus\"].",
        read_dir=str(sheet.parent), timeout=300)
    subjects = [str(s) for s in extract_json(answer)]
    plan["subjects"] = subjects
    save_plan(slug, plan)
    return subjects


def _facts(plan: dict, pictures: int, pages: int, trim: str, formats: list[str]) -> dict:
    w, h = specs.parse_trim(trim)
    return {"theme": plan["theme"], "audience": plan["audience"], "ages": plan.get("ages"),
            "style": plan.get("style", ""), "picture_count": pictures, "pages": pages,
            "trim": f"{w:g} x {h:g} in", "subjects": plan.get("subjects", []),
            "layout": "one picture per right-hand page with a blank back, 'This book belongs to' page, "
                      "copyright page, colour test page",
            "formats": formats}


async def _calculator(fmt: str, trim: str, pages: int) -> dict | None:
    """Cover size from KDP's calculator (cached). Hardcover must be confirmed; paperback falls back to the formula."""
    from .telegram_bot import ask_me
    cache = settings.read_json(settings.DATA / "cover_calc.json", {})
    key = f"{fmt}|{trim}|{pages}"
    if key in cache:
        return cache[key]
    result = None
    try:
        result = await covers.read_kdp_calculator(fmt, trim, pages)
    except NeedsHuman:
        raise
    except Exception as exc:
        log.warning("Cover calculator failed: %s", exc)
    if result is None and fmt == "hardcover":
        w, h = specs.parse_trim(trim)
        a = await ask_me(
            f"📐 I couldn't read KDP's cover calculator. Please open kdp.amazon.com/cover-calculator, choose "
            f"Hardcover, Black & white, White paper, trim {w:g} x {h:g} in, {pages} pages, inches - then type the "
            "full cover size like: 17.94 x 12.18 (and spine if shown, like: 17.94 x 12.18 spine 0.25)",
            about=f"calc:{key}")
        nums = re.findall(r"\d+(?:\.\d+)?", (a or {}).get("text") or "")
        if len(nums) >= 2:
            result = {"width": float(nums[0]), "height": float(nums[1])}
            if len(nums) >= 3:
                result["spine"] = float(nums[2])
    if result:
        cache[key] = result
        settings.write_json(settings.DATA / "cover_calc.json", cache)
    return result


async def build_book(slug: str, feedback: str | None = None) -> dict:
    """Research, listing, interiors and covers for one book. Safe to run again."""
    book = jobs.load_book(slug)
    plan = load_plan(slug)
    pictures = picture_sources(slug)
    if not pictures:
        raise NeedsHuman("No pictures found for this book.")
    formats = list(plan.get("formats", ["paperback", "hardcover"]))

    # Trim sizes
    src_size = None
    if plan.get("source") == "inbox":
        rep = settings.read_json(book_path(slug, "check.json"), {})
        src_size = tuple(rep["size_in"])
    pb_trim = plan.get("trim") or (specs.trim_key(*src_size) if src_size else settings.load_config()["books"]["default_trim"])
    if pb_trim not in specs.paperback_trims():
        pb_trim = specs.nearest_trim(*specs.parse_trim(pb_trim), list(specs.paperback_trims()))
    hc_trim = pb_trim if pb_trim in specs.hardcover_trims() else specs.nearest_trim(*specs.parse_trim(pb_trim), specs.hardcover_trims())
    pages = 4 + 2 * len(pictures) + 2
    notes = []
    if "hardcover" in formats and specs.page_count_ok("hardcover", hc_trim, pages):
        notes.append(f"Hardcover skipped: {specs.page_count_ok('hardcover', hc_trim, pages)}.")
        formats.remove("hardcover")

    # What's in the pictures, research and listing
    await find_subjects(slug, pictures)
    plan = load_plan(slug)
    data = research.cached(plan["seed"]) or await research.research(plan["seed"], plan.get("audience_words"))
    old = settings.read_json(book_path(slug, "listing.json"), None)
    facts = _facts(plan, len(pictures), pages, pb_trim, formats)
    if old and not feedback:
        lst = old
    else:
        lst = await listing.write_listing(facts, research.research_notes(data), feedback=feedback, previous=old)
    prices = {"paperback": listing.price_options("paperback", pb_trim, pages, data["summary"]["prices"])}
    if "hardcover" in formats:
        pb = prices["paperback"]["options"][prices["paperback"]["recommended"]]["price"]
        prices["hardcover"] = listing.price_options("hardcover", hc_trim, pages, None, pb)
        prices["hardcover"]["recommended"] = 0
    lst.update({"price_options": prices, "facts": facts, "research_seed": data["seed"],
                "research_summary": data["summary"]})
    settings.write_json(book_path(slug, "listing.json"), lst)

    # Interiors
    built = {"formats": {}, "notes": notes}
    for fmt in formats:
        trim = pb_trim if fmt == "paperback" else hc_trim
        if fmt == "hardcover" and trim == pb_trim and "paperback" in built["formats"]:
            built["formats"][fmt] = dict(built["formats"]["paperback"])
            continue
        res = await interior.build_interior(lst["title"], lst.get("subtitle", ""), pictures, trim,
                                            book_path(slug, f"interior_{fmt}.pdf"))
        final = await asyncio.to_thread(pdftools.check, res["path"], fmt, True, 300, 100, True)
        res["final_check"] = [i for i in final.issues]
        built["formats"][fmt] = res

    # Covers (2-3 real pictures from the book)
    first = built["formats"][formats[0]]
    pic_pages = first["picture_pages"]
    chosen = [pic_pages[0], pic_pages[len(pic_pages) // 2], pic_pages[-1]] if len(pic_pages) >= 3 else pic_pages
    cover_pics = [pdftools.crop_picture_png(first["path"], n - 1, 300, book_path(slug, "cover_pics", f"pic{i}.png"))
                  for i, n in enumerate(chosen)]
    palette = lst.get("cover_palette")
    for fmt in formats:
        res = built["formats"][fmt]
        calc = await _calculator(fmt, res["trim"], res["pages"])
        cov = await covers.make_cover(fmt, res["trim"], res["pages"], book_path(slug, f"cover_{fmt}.pdf"),
                                      lst["title"], lst.get("subtitle", ""), settings.load_config()["agent"]["pen_name"],
                                      lst.get("badge", ""), lst.get("blurb", ""), cover_pics, theme=plan["theme"],
                                      calculator=calc, palette=palette)
        res["cover"] = cov
        if fmt == "hardcover" and not calc:
            raise NeedsHuman("Hardcover cover size is not confirmed by KDP's cover calculator.")
    # Previews for Telegram
    pv = []
    fmt0 = formats[0]
    pv.append(str(covers.front_preview(Path(built["formats"][fmt0]["cover"]["path"]), built["formats"][fmt0]["cover"]["size"],
                                       book_path(slug, "previews", "front.png"))))
    pv.append(str(covers.wrap_preview(Path(built["formats"][fmt0]["cover"]["path"]), book_path(slug, "previews", "wrap.png"))))
    for i, n in enumerate(chosen[:3]):
        pv.append(str(interior.save_png_preview(Path(first["path"]), n, book_path(slug, "previews", f"sample{i}.png"))))
    built["previews"] = pv
    built["trims"] = {"paperback": pb_trim, "hardcover": hc_trim if "hardcover" in formats else None}
    settings.write_json(book_path(slug, "build.json"), built)
    jobs.update_book(slug, title=lst["title"])
    return built


# ------------------------------------------------------------ stage: built (your review)

def _review_text(slug: str) -> str:
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    built = settings.read_json(book_path(slug, "build.json"), {})
    lines = [listing.listing_text(lst, lst.get("price_options"))]
    for fmt, res in built.get("formats", {}).items():
        lines.append(f"\n📄 {fmt.title()}: {res['trim']} in, {res['pages']} pages")
        for i in res.get("final_check", []):
            icon = {"ok": "✅", "you": "❗", "fix": "🔧", "info": "ℹ️"}[i["level"]]
            lines.append(f"  {icon} {i['text']}")
        for c in res.get("cover", {}).get("checks", []):
            lines.append(f"  {'✅' if c['ok'] else '❔' if c['ok'] is None else '❗'} {c['text']}")
    lines += [f"ℹ️ {n}" for n in built.get("notes", [])]
    return "\n".join(lines)


@stage_worker("built")
async def review_worker(book: dict) -> str | None:
    from .telegram_bot import ask_me, notify, send_photos
    slug = book["slug"]
    built = settings.read_json(book_path(slug, "build.json"), {})
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    await send_photos([Path(p) for p in built["previews"]])
    for chunk in _chunks(_review_text(slug)):
        await notify(chunk)
    pb = lst["price_options"]["paperback"]
    buttons = [f"Approve {chr(65 + i)} (${o['price']:.2f})" for i, o in enumerate(pb["options"])] + ["Change something"]
    answer = await ask_me(f"👆 '{lst['title']}' is ready to upload (nothing is uploaded yet). Approve with a paperback "
                          "price, or tell me what to change:", buttons=buttons, typed_for=["Change something"],
                          about=f"review:{slug}")
    if not answer:
        return None
    if answer["choice"] and answer["choice"].startswith("Approve"):
        idx = ord(answer["choice"].split()[1]) - 65
        chosen = {"paperback": pb["options"][idx]}
        if "hardcover" in lst["price_options"]:
            hc = lst["price_options"]["hardcover"]
            chosen["hardcover"] = hc["options"][hc["recommended"]]
        lst["prices"] = {fmt: {"US": o["price"], "royalty_usd": o["royalty"], "printing_cost": o["printing_cost"]}
                         for fmt, o in chosen.items()}
        settings.write_json(book_path(slug, "listing.json"), lst)
        taste.record("book", "approved", lst["title"], price=chosen["paperback"]["price"])
        return "listed"
    feedback = answer.get("text") or ""
    taste.record("listing", "changed", lst["title"], reason=feedback)
    await notify("✏️ Got it - working on your change. I'll send the new version when it's ready.")
    await build_book(slug, feedback=feedback)
    return None   # stay in 'built' - the new version is sent next time round


def _chunks(text: str, size: int = 3800) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    return out + ([cur] if cur else [])

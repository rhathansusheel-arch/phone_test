"""KDP uploader. Amazon has no upload API, so this fills KDP's website like a person would.

Safety:
- Weekly limit guard before anything (Hard rule 1). A creation slot is counted BEFORE the first save.
- Finds fields by their visible labels. If a field can't be found: stop and tell you (never guess).
- AI disclosure always answered Yes (Hard rule 2) - Save as Draft is refused if it wasn't answered.
- Login / code / CAPTCHA: stop, screenshot, wait for you (Hard rule 6).
- Only "Save as Draft" while filling. "Publish" only after you tap Publish on Telegram.
- Human pace: 2-5 s pauses.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import decisions, jobs, limits, settings, specs, taste
from .browser import goto, guard, human_pause, profile_session, screenshot
from .errors import FieldNotFound, NeedsHuman, StopBook
from .jobs import stage_worker

log = logging.getLogger("kdp")

# Visible texts on KDP's pages. If KDP renames something, change it here.
L = {
    "title": ["Book Title", "Title"],
    "subtitle": ["Subtitle"],
    "first_name": ["First name", "First Name"],
    "last_name": ["Last name", "Last Name"],
    "description": ["Description"],
    "own_rights": ["I own the copyright and I hold the necessary publishing rights",
                   "I own the copyright"],
    "explicit_no": "No",
    "reading_min": ["Minimum", "Min"],
    "reading_max": ["Maximum", "Max"],
    "marketplace": ["Primary marketplace", "Primary Marketplace"],
    "choose_categories": ["Choose categories", "Choose Categories", "Edit categories"],
    "save_categories": ["Save categories", "Save"],
    "keywords_heading": "Keywords",
    "save_continue": ["Save and Continue"],
    "save_draft": ["Save as Draft"],
    "publish": {"paperback": ["Publish Your Paperback Book"], "hardcover": ["Publish Your Hardcover Book"]},
    "free_isbn": ["Assign me a free KDP ISBN", "Get a free KDP ISBN"],
    "assign_isbn": ["Assign ISBN"],
    "ink_paper": ["Black & white interior with white paper"],
    "select_trim": ["Select a different size", "Select a Different Size"],
    "no_bleed": ["No Bleed"],
    "finish": {"matte": ["Matte"], "glossy": ["Glossy"]},
    "upload_manuscript": ["Upload paperback manuscript", "Upload hardcover manuscript", "Upload manuscript"],
    "cover_have": ["Upload a cover you already have (print-ready PDF only)", "Upload a cover you already have"],
    "upload_cover": ["Upload your cover file", "Upload cover"],
    "ai_question": "Did you use AI tools in creating texts, images, and/or translations",
    "ai_yes": "Yes",
    "launch_previewer": ["Launch Previewer"],
    "approve_preview": ["Approve"],
    "all_territories": ["All territories (worldwide rights)", "All territories"],
    "amazon_com": "Amazon.com",
}

PAGES = {"details": "details", "content": "content", "pricing": "pricing"}


def kdp_url(path: str) -> str:
    return settings.load_config()["browser"]["kdp_base_url"].rstrip("/") + "/" + path.lstrip("/")


def answers() -> dict:
    return settings.read_json(settings.DATA / "answers.json", {})


def save_answer(key: str, value) -> None:
    data = answers()
    data[key] = value
    settings.write_json(settings.DATA / "answers.json", data)


class Form:
    """Find things on a KDP page by the words you can see. Never guesses."""

    def __init__(self, page, slug: str, fmt: str):
        self.page, self.slug, self.fmt = page, slug, fmt
        self.shots: list[Path] = []

    async def _first(self, candidates):
        for c in candidates:
            try:
                if await c.count() > 0 and await c.first.is_visible():
                    return c.first
            except Exception:
                continue
        return None

    async def field(self, names: list[str] | str, required: bool = True):
        names = [names] if isinstance(names, str) else names
        p = self.page
        cands = []
        for n in names:
            cands += [p.get_by_label(n, exact=True), p.get_by_label(n), p.get_by_placeholder(n),
                      p.get_by_role("textbox", name=n), p.get_by_role("combobox", name=n)]
        found = await self._first(cands)
        if found is None and required:
            await self.stop(f"I couldn't find the field '{names[0]}'")
        return found

    async def button(self, names: list[str] | str, required: bool = True):
        names = [names] if isinstance(names, str) else names
        p = self.page
        cands = []
        for n in names:
            cands += [p.get_by_role("button", name=n, exact=True), p.get_by_role("button", name=n),
                      p.get_by_role("link", name=n), p.locator(f"input[type=submit][value='{n}']")]
        found = await self._first(cands)
        if found is None and required:
            await self.stop(f"I couldn't find the button '{names[0]}'")
        return found

    async def option(self, names: list[str] | str, required: bool = True):
        """A radio button or checkbox, by its label."""
        names = [names] if isinstance(names, str) else names
        p = self.page
        cands = []
        for n in names:
            cands += [p.get_by_role("radio", name=n), p.get_by_label(n), p.get_by_role("checkbox", name=n)]
        found = await self._first(cands)
        if found is None and required:
            await self.stop(f"I couldn't find the option '{names[0]}'")
        return found

    async def fill(self, names, value: str, required: bool = True) -> bool:
        f = await self.field(names, required)
        if f is None:
            return False
        await f.fill("")
        await f.type(value, delay=25)
        await human_pause(0.4)
        return True

    async def select(self, names, option_text: str, required: bool = True) -> bool:
        f = await self.field(names, required)
        if f is None:
            return False
        try:
            await f.select_option(label=option_text)
        except Exception:
            await f.click()
            await human_pause(0.3)
            opt = await self._first([self.page.get_by_role("option", name=option_text),
                                     self.page.get_by_text(option_text, exact=True)])
            if opt is None:
                if required:
                    await self.stop(f"I couldn't choose '{option_text}' in '{names[0] if isinstance(names, list) else names}'")
                return False
            await opt.click()
        await human_pause(0.4)
        return True

    async def choose(self, names, required: bool = True) -> bool:
        o = await self.option(names, required)
        if o is None:
            return False
        try:
            await o.check()
        except Exception:
            await o.click()
        await human_pause(0.4)
        return True

    async def click(self, names, required: bool = True) -> bool:
        b = await self.button(names, required)
        if b is None:
            return False
        await b.click()
        await human_pause()
        await guard(self.page, f"working on KDP ({self.fmt})")
        return True

    async def upload(self, names, file: Path) -> None:
        b = await self.button(names)
        async with self.page.expect_file_chooser() as fc:
            await b.click()
        chooser = await fc.value
        await chooser.set_files(str(file))
        await human_pause()

    async def shot(self, name: str) -> Path:
        folder = jobs.book_dir(self.slug) / "kdp_screens"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.fmt}_{len(self.shots) + 1:02d}_{name}.png"
        await self.page.screenshot(path=str(path), full_page=True)
        self.shots.append(path)
        return path

    async def stop(self, why: str):
        from .telegram_bot import notify, send_photos
        shot = await screenshot(self.page, "field-not-found")
        await send_photos([shot])
        await notify(f"🛑 KDP ({self.fmt}): {why}. KDP may have changed its page, so I stopped instead of guessing. "
                     f"Nothing was published. Tell me what you see, or fix it by hand, then /unhold {self.slug}.")
        raise FieldNotFound(why)

    async def body_text(self) -> str:
        return await self.page.inner_text("body")


# ------------------------------------------------------------ section helpers

async def _fill_description(form: Form, text: str) -> None:
    page = form.page
    # KDP's description box is a rich-text editor (maybe inside an iframe).
    for frame in page.frames:
        try:
            body = frame.locator("body[contenteditable=true], [contenteditable=true]")
            if frame != page.main_frame and await body.count():
                await body.first.click()
                await body.first.fill("")
                await body.first.type(text, delay=5)
                return
        except Exception:
            continue
    editable = page.locator("[contenteditable=true]")
    if await editable.count():
        await editable.first.click()
        await editable.first.fill("")
        await editable.first.type(text, delay=5)
        return
    await form.fill(L["description"], text)


async def _fill_keywords(form: Form, keywords: list[str]) -> None:
    heading = form.page.get_by_text(L["keywords_heading"], exact=False).first
    if not await heading.count():
        await form.stop("I couldn't find the Keywords section")
    boxes = heading.locator("xpath=following::input[@type='text' or not(@type)]")
    if await boxes.count() < 7:
        await form.stop("I couldn't find the 7 keyword boxes")
    for i, kw in enumerate(keywords[:7]):
        box = boxes.nth(i)
        await box.fill("")
        await box.type(kw, delay=20)
        await human_pause(0.3)


async def _set_categories(form: Form, categories: list[str]) -> None:
    from .telegram_bot import ask_me
    if not await form.click(L["choose_categories"], required=False):
        await form.stop("I couldn't find 'Choose categories'")
    ok = True
    try:
        for path in categories:
            parts = [p.strip() for p in path.split(">")]
            for part in parts[:-1]:
                sel = form.page.locator("select").filter(has=form.page.locator(f"option:text-is('{part}')"))
                if await sel.count():
                    await sel.first.select_option(label=part)
                else:
                    await form.page.get_by_text(part, exact=True).first.click(timeout=4000)
                await human_pause(0.5)
            leaf = await form.option(parts[-1], required=False)
            if leaf is None:
                ok = False
                break
            await leaf.check()
            await human_pause(0.5)
    except Exception as exc:
        log.warning("Category picking failed: %s", exc)
        ok = False
    if not ok:
        shot = await form.shot("categories")
        answer = await ask_me("📂 I couldn't pick the categories by myself. In the KDP window on your PC, please choose:\n• "
                              + "\n• ".join(categories) + "\nthen tap Done (don't click Save as Draft or Publish).",
                              buttons=["Done", "Stop"], photos=[shot], about=f"categories:{form.slug}")
        if not answer or answer["choice"] != "Done":
            raise StopBook("Stopped at categories.")
        return
    await form.click(L["save_categories"], required=False)


async def _answer_ai(form: Form) -> bool:
    """Hard rule 2: answer KDP's AI question honestly. Returns True when answered Yes."""
    text = await form.body_text()
    if L["ai_question"].lower()[:40] not in text.lower():
        return False
    cfg = settings.load_config().get("ai_disclosure", {})
    question = form.page.get_by_text(L["ai_question"], exact=False).first
    yes = question.locator("xpath=following::*[self::input[@type='radio']][1]")
    labelled = form.page.get_by_role("radio", name=L["ai_yes"], exact=True)
    target = labelled.first if await labelled.count() else yes
    await target.check()
    await human_pause(0.5)
    tool = cfg.get("tool", "Claude (Anthropic)")
    for kind in ("Images", "Texts", "Translations"):
        level = cfg.get(kind.lower(), "None")
        sel = form.page.get_by_label(kind, exact=False)
        if await sel.count():
            try:
                await sel.first.select_option(label=level)
            except Exception:
                pass
            await human_pause(0.3)
            if level != "None":
                tool_box = sel.first.locator("xpath=following::input[@type='text'][1]")
                if await tool_box.count():
                    await tool_box.fill(tool)
    if not await target.is_checked():
        await form.stop("The AI-generated content answer didn't stay on Yes")
    return True


# ------------------------------------------------------------ before the first upload

async def ensure_ready() -> None:
    """Things that must be true before ANY upload: KDP numbers confirmed, low-content answer known."""
    from .telegram_bot import ask_me, notify
    if not specs.verified():
        await notify("📏 Before my first upload I'm re-reading KDP's own help pages to confirm sizes and prices...")
        found, diffs = await specs.check_from_kdp()
        text = "No differences - my numbers match KDP." if not diffs else "Differences found:\n" + "\n".join(diffs[:30])
        a = await ask_me(f"📏 KDP check:\n{text}\n\nSave KDP's numbers and continue?",
                         buttons=["Save and continue", "Stop"], about="checkspecs")
        if not a or a["choice"] != "Save and continue":
            raise NeedsHuman("KDP numbers not confirmed (/checkspecs).")
        specs.save_checked(found)
    if "low_content" not in answers():
        from .claude_cli import ask_claude
        summary = await ask_claude(
            "Read Amazon KDP's help page about low-content books (search kdp.amazon.com help for 'low-content'). "
            "In 5 short lines of simple English: what counts as low-content, whether colouring books count, "
            "and what changes if a book is marked low-content (ISBN, previews). Only use kdp.amazon.com.",
            tools=["WebFetch", "WebSearch"], timeout=600)
        a = await ask_me("❓ KDP asks if a book is 'low-content'. Here's what KDP's help page says:\n\n"
                         f"{summary}\n\nHow should I answer for your colouring books? (I'll remember.)",
                         buttons=["Not low-content (use a free ISBN)", "Low-content"], about="low-content")
        if not a:
            raise NeedsHuman("Low-content question not answered.")
        save_answer("low_content", a["choice"] == "Low-content")


# ------------------------------------------------------------ create / update a draft

def _kdp_state(slug: str) -> dict:
    return (jobs.load_book(slug) or {}).get("kdp", {})


def _set_kdp(slug: str, fmt: str, **fields) -> None:
    book = jobs.load_book(slug)
    kdp = book.get("kdp", {})
    kdp.setdefault(fmt, {}).update(fields)
    jobs.update_book(slug, kdp=kdp)


async def _open_existing(form: Form, title: str, link_names: list[str]) -> bool:
    """On the Bookshelf, find this book's row and click one of its links."""
    await goto(form.page, kdp_url("bookshelf"), "opening your KDP Bookshelf")
    row = form.page.locator("tr, div[class*='row'], div[class*='Row']").filter(has_text=title)
    if not await row.count():
        return False
    for n in link_names:
        link = row.first.get_by_role("link", name=n)
        btn = row.first.get_by_role("button", name=n)
        for cand in (link, btn):
            if await cand.count():
                await cand.first.click()
                await human_pause()
                await guard(form.page, "opening the book on KDP")
                return True
    return False


async def fill_format(slug: str, fmt: str) -> dict:
    """Fill KDP for one format and click Save as Draft. Never publishes."""
    from .pipeline import book_path, load_plan
    book = jobs.load_book(slug)
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    built = settings.read_json(book_path(slug, "build.json"), {})
    plan = load_plan(slug)
    res = built["formats"][fmt]
    cfg = settings.load_config()
    state = _kdp_state(slug).get(fmt, {})
    is_new = not state.get("created")

    if is_new and limits.slots_left(fmt, "created") <= 0:
        return {"queued": True, "why": f"No {fmt} creation slot left this week. Next reset: {limits.reset_text()}"}

    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        form = Form(page, slug, fmt)
        # 1. Open the right page
        if is_new:
            opened = False
            if fmt == "hardcover" and _kdp_state(slug).get("paperback", {}).get("created"):
                opened = await _open_existing(form, lst["title"], ["Create hardcover", "+ Create hardcover"])
            if not opened:
                await goto(page, kdp_url(f"title-setup/{fmt}/new/details"), f"starting a new {fmt}")
        else:
            if not await _open_existing(form, lst["title"], [f"Edit {fmt} details", "Continue setup", "Edit"]):
                await form.stop(f"I couldn't find '{lst['title']}' on your Bookshelf")

        # 2. Details
        await form.select(["Language"], "English", required=False)
        await form.fill(L["title"], lst["title"])
        await form.fill(L["subtitle"], lst.get("subtitle", ""), required=bool(lst.get("subtitle")))
        if cfg["agent"]["author_first_name"]:
            await form.fill(L["first_name"], cfg["agent"]["author_first_name"])
        await form.fill(L["last_name"], cfg["agent"]["author_last_name"])
        await _fill_description(form, lst["description"])
        await form.choose(L["own_rights"])
        explicit = page.get_by_text("Sexually Explicit", exact=False)
        if await explicit.count():
            no = explicit.first.locator("xpath=following::input[@type='radio'][2]")
            labelled_no = page.get_by_role("radio", name="No", exact=True)
            await (labelled_no.first if await labelled_no.count() else no).check()
        age = lst.get("reading_age")
        if age:
            await form.select(L["reading_min"], str(age["min"]), required=False)
            await form.select(L["reading_max"], str(age["max"]), required=False)
        await form.select(L["marketplace"], L["amazon_com"], required=False)
        await _set_categories(form, lst["categories"])
        await _fill_keywords(form, lst["keywords"])
        ai_done = await _answer_ai(form)
        await form.shot("details")
        if is_new:
            limits.use_slot(fmt, "created", slug)      # counted BEFORE the first save (Hard rule 1)
            decisions.log({"type": "kdp_slot", "slot": "created", "format": fmt, "slug": slug})
        await form.click(L["save_continue"])
        _set_kdp(slug, fmt, created=True, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        # 3. Content
        body = await form.body_text()
        if answers().get("low_content"):
            await form.choose(["This is a low-content book", "Low-content book"], required=True)
        elif not re.search(r"ISBN\s*:?\s*\d{3}-?\d", body) and "Assign me a free" in body:
            await form.click(L["free_isbn"])
            await form.click(L["assign_isbn"], required=False)
        await form.choose(L["ink_paper"])
        w, h = specs.parse_trim(res["trim"])
        trim_text = f"{w:g} x {h:g} in"
        if await form.click(L["select_trim"], required=False):
            size = await form._first([page.get_by_role("button", name=trim_text), page.get_by_role("radio", name=trim_text),
                                      page.get_by_text(trim_text, exact=True)])
            if size is None:
                await form.stop(f"I couldn't find trim size {trim_text}")
            await size.click()
            await human_pause()
        elif trim_text not in await form.body_text():
            await form.stop(f"I couldn't set the trim size to {trim_text}")
        await form.choose(L["no_bleed"], required=fmt == "paperback")
        finish = plan.get("cover_finish", cfg["books"]["cover_finish"])
        await form.choose(L["finish"][finish])
        await form.upload(L["upload_manuscript"], book_path(slug, f"interior_{fmt}.pdf"))
        await form.choose(L["cover_have"], required=False)
        await form.upload(L["upload_cover"], book_path(slug, f"cover_{fmt}.pdf"))
        await _wait_processing(form)
        ai_done = ai_done or await _answer_ai(form)
        if not ai_done:
            await form.stop("I couldn't find KDP's AI-generated content question, so I won't save")
        _set_kdp(slug, fmt, ai_disclosed=True)
        warnings = await _previewer(form)
        await form.shot("content")
        await form.click(L["save_continue"])

        # 4. Rights and pricing
        await form.choose(L["all_territories"])
        await form.select(L["marketplace"], L["amazon_com"], required=False)
        price = lst["prices"][fmt]["US"]
        row = page.get_by_text(L["amazon_com"], exact=True).first
        price_box = row.locator("xpath=following::input[1]")
        if not await row.count() or not await price_box.count():
            await form.stop("I couldn't find the Amazon.com price box")
        await price_box.fill(f"{price:.2f}")
        await price_box.press("Tab")
        await human_pause()
        row_text = await row.locator("xpath=ancestor::*[self::tr or self::div][1]").inner_text()
        found = re.findall(r"\$\s?(\d+\.\d\d)", row_text)
        kdp_royalty = float(found[-1]) if found else None
        await form.shot("pricing")
        # 5. SAVE AS DRAFT ONLY
        await form.click(L["save_draft"])
        await form.shot("saved")
    _set_kdp(slug, fmt, draft_saved=True, price=price, kdp_royalty=kdp_royalty,
             screenshots=[str(p) for p in form.shots])
    return {"queued": False, "price": price, "kdp_royalty": kdp_royalty, "warnings": warnings,
            "screens": [str(p) for p in form.shots]}


async def _wait_processing(form: Form, seconds: int = 300) -> None:
    for _ in range(seconds // 5):
        text = (await form.body_text()).lower()
        if "processing" not in text and "uploading" not in text:
            break
        await form.page.wait_for_timeout(5000)
    text = await form.body_text()
    m = re.search(r"(error[^\n]{0,200})", text, re.I)
    if m and "no error" not in m.group(1).lower():
        await form.shot("upload-error")
        await form.stop(f"KDP showed an error after uploading: {m.group(1)[:200]}")


async def _previewer(form: Form) -> list[str]:
    """Open KDP's previewer, read any warnings, approve if there are no errors."""
    if not await form.click(L["launch_previewer"], required=False):
        return ["Previewer button not found - check the preview by hand"]
    for _ in range(36):
        if await form.button(L["approve_preview"], required=False):
            break
        await form.page.wait_for_timeout(5000)
    text = await form.body_text()
    issues = re.findall(r"([^\n]*(?:issue|warning|error)[^\n]*)", text, re.I)
    await form.shot("previewer")
    if re.search(r"\b\d+\s+errors?\b|must be fixed", text, re.I):
        await form.stop("KDP's previewer found errors: " + "; ".join(issues[:5]))
    await form.click(L["approve_preview"])
    return issues[:10]


# ------------------------------------------------------------ publish / status

async def publish_format(slug: str, fmt: str) -> str:
    """Click Publish for one format (only after your Approve). Returns 'in_review' or 'queued'."""
    if limits.slots_left(fmt, "submitted") <= 0:
        return "queued"
    from .pipeline import book_path
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        form = Form(page, slug, fmt)
        if not await _open_existing(form, lst["title"], [f"Edit {fmt} rights & pricing", "Continue setup",
                                                          f"Edit {fmt} details", "Edit"]):
            await form.stop(f"I couldn't find '{lst['title']}' on your Bookshelf")
        for _ in range(3):   # move through to the pricing page
            if await form.button(L["publish"][fmt], required=False):
                break
            await form.click(L["save_continue"])
        if not await form.button(L["publish"][fmt], required=False):
            await form.stop("I couldn't find the Publish button")
        limits.use_slot(fmt, "submitted", slug)       # counted BEFORE clicking Publish
        decisions.log({"type": "kdp_slot", "slot": "submitted", "format": fmt, "slug": slug})
        await form.click(L["publish"][fmt])
        await form.shot("published")
        text = (await form.body_text()).lower()
    if "review" not in text and "submitted" not in text and "publishing" not in text:
        raise NeedsHuman(f"I clicked Publish for the {fmt} but didn't see 'In review'. Please check KDP.")
    _set_kdp(slug, fmt, submitted=True, submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return "in_review"


async def bookshelf_status(title: str) -> dict:
    """Read this book's row on the Bookshelf: {'paperback': 'IN REVIEW' / 'LIVE' / ..., 'links': [...]}."""
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await goto(page, kdp_url("bookshelf"), "checking your Bookshelf")
        row = page.locator("tr, div[class*='row'], div[class*='Row']").filter(has_text=title)
        if not await row.count():
            return {}
        text = await row.first.inner_text()
        links = [await a.get_attribute("href") for a in await row.first.locator("a[href*='amazon.']").all()]
    out = {"links": [l for l in links if l]}
    for fmt in ("paperback", "hardcover"):
        m = re.search(fmt + r"[\s\S]{0,200}?\b(LIVE|IN REVIEW|PUBLISHING|DRAFT|BLOCKED|ACTION REQUIRED|IN PROGRESS)\b",
                      text, re.I)
        if m:
            out[fmt] = m.group(1).upper()
    return out


# ------------------------------------------------------------ Hard rule 7: live books

LOCKED_FIELDS = {"title", "subtitle", "author", "author_first_name", "author_last_name", "pen_name"}
EDITABLE_LIVE = {"description", "keywords", "categories", "price"}


class LockedField(Exception):
    pass


def check_live_changes(slug: str, changes: dict) -> None:
    """Refuse changes KDP doesn't allow on a live book."""
    book = jobs.load_book(slug)
    if book and book["stage"] in ("in_review", "live"):
        locked = set(changes) & LOCKED_FIELDS
        if locked:
            raise LockedField(f"'{', '.join(sorted(locked))}' can't be changed on a published book "
                              "(KDP locks title, subtitle and author).")
        unknown = set(changes) - EDITABLE_LIVE
        if unknown:
            raise LockedField(f"I only change description, keywords, categories and price on live books, "
                              f"not {', '.join(sorted(unknown))}.")


async def update_live(slug: str, changes: dict) -> None:
    """Update description/keywords/categories/price of a published book (after your Approve)."""
    check_live_changes(slug, changes)
    from .pipeline import book_path
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    book = jobs.load_book(slug)
    for fmt in [f for f in ("paperback", "hardcover") if book.get("kdp", {}).get(f, {}).get("submitted")]:
        async with profile_session("kdp") as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            form = Form(page, slug, fmt)
            if not await _open_existing(form, lst["title"], [f"Edit {fmt} details", "Edit"]):
                await form.stop(f"I couldn't find '{lst['title']}' on your Bookshelf")
            if "description" in changes:
                await _fill_description(form, changes["description"])
            if "keywords" in changes:
                await _fill_keywords(form, changes["keywords"])
            if "categories" in changes:
                await _set_categories(form, changes["categories"])
            await form.click(L["save_continue"])
            await form.click(L["save_continue"])
            if "price" in changes and fmt in changes["price"]:
                row = page.get_by_text(L["amazon_com"], exact=True).first
                box = row.locator("xpath=following::input[1]")
                await box.fill(f"{changes['price'][fmt]:.2f}")
                await box.press("Tab")
            await form.shot("update")
            await form.click(L["publish"][fmt])   # re-publishing an update does not use a weekly slot
    lst.update({k: v for k, v in changes.items() if k != "price"})
    if "price" in changes:
        for fmt, p in changes["price"].items():
            lst.setdefault("prices", {}).setdefault(fmt, {})["US"] = p
    settings.write_json(book_path(slug, "listing.json"), lst)


# ------------------------------------------------------------ stage workers

@stage_worker("listed")
async def upload_worker(book: dict) -> str | None:
    """Fill KDP as drafts: paperback first, then hardcover."""
    from .pipeline import book_path, load_plan
    from .telegram_bot import ask_me, notify, send_files
    slug = book["slug"]
    plan = load_plan(slug)
    built = settings.read_json(book_path(slug, "build.json"), {})
    formats = list(built["formats"])
    if plan.get("manual_upload"):
        files = [book_path(slug, f"{kind}_{fmt}.pdf") for fmt in formats for kind in ("interior", "cover")]
        await send_files(files, caption=f"📦 Files for '{book['title']}' (you're uploading this one by hand). "
                                        "The listing text is in the message above.")
        a = await ask_me("Tap when you've published it on KDP, so I can count this week's slots and track it.",
                         buttons=["I published it", "Not yet"], about=f"manual:{slug}")
        if not a or a["choice"] != "I published it":
            jobs.snooze(slug, 6 * 3600)
            return None
        for fmt in formats:
            for kind in ("created", "submitted"):
                try:
                    limits.use_slot(fmt, kind, slug)
                except limits.NoSlotLeft:
                    pass
            _set_kdp(slug, fmt, created=True, submitted=True, manual=True)
        return "in_review"
    await ensure_ready()
    results = {}
    for fmt in formats:
        if _kdp_state(slug).get(fmt, {}).get("draft_saved"):
            continue
        r = await fill_format(slug, fmt)
        if r.get("queued"):
            await notify(f"🗓 '{book['title']}' {fmt}: {r['why']} I've queued it.")
            jobs.snooze(slug, (limits.next_reset() - datetime.now(timezone.utc)).total_seconds() + 120)
            return None
        results[fmt] = r
    return "drafted"


@stage_worker("drafted")
async def drafted_worker(book: dict) -> str | None:
    return "waiting_approval"


@stage_worker("waiting_approval")
async def approval_worker(book: dict) -> str | None:
    """Send you every KDP screenshot + the price table, then wait for Publish / Fix / Cancel."""
    from .pipeline import book_path
    from .telegram_bot import ask_me, notify, send_photos
    slug = book["slug"]
    kdp = jobs.load_book(slug).get("kdp", {})
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    if jobs.load_book(slug).get("publish_approved"):
        return await _publish_all(slug, kdp)
    shots = [p for fmt in kdp for p in kdp[fmt].get("screenshots", [])]
    await send_photos(shots[:20])
    lines = [f"📋 '{lst['title']}' is saved as a DRAFT on KDP (not published)."]
    for fmt, s in kdp.items():
        p = lst["prices"][fmt]
        lines.append(f"• {fmt.title()}: ${p['US']:.2f} → royalty ${p['royalty_usd']:.2f} (my maths), "
                     f"KDP shows {('$%.2f' % s['kdp_royalty']) if s.get('kdp_royalty') else 'n/a'}; "
                     f"AI disclosure answered Yes: {'✅' if s.get('ai_disclosed') else '❗'}")
    lines.append("\nSlots left this week:\n" + limits.summary())
    await notify("\n".join(lines))
    a = await ask_me("Publish it?", buttons=["Publish", "Fix something", "Cancel"], typed_for=["Fix something"],
                     about=f"publish:{slug}")
    if not a:
        return None
    if a["choice"] == "Publish":
        taste.record("publish", "approved", lst["title"])
        jobs.update_book(slug, publish_approved=True)
        return await _publish_all(slug, kdp)
    if a["choice"] == "Cancel":
        taste.record("publish", "rejected", lst["title"], reason="cancelled")
        raise StopBook("You cancelled publishing. The drafts stay on KDP.")
    taste.record("publish", "changed", lst["title"], reason=a.get("text"))
    await notify("✏️ I'll make that change and update the drafts (this doesn't use new slots).")
    from .pipeline import build_book
    await build_book(slug, feedback=a.get("text"))
    for fmt in kdp:
        _set_kdp(slug, fmt, draft_saved=False)
    jobs.set_stage(slug, "built", note=f"fix: {a.get('text')}")
    return None


async def _publish_all(slug: str, kdp: dict) -> str | None:
    from .telegram_bot import notify
    pending = [f for f in kdp if not kdp[f].get("submitted")]
    for fmt in pending:
        result = await publish_format(slug, fmt)
        if result == "queued":
            await notify(f"🗓 No {fmt} submission slot left this week - I'll publish it after {limits.reset_text()}.")
            jobs.snooze(slug, (limits.next_reset() - datetime.now(timezone.utc)).total_seconds() + 120)
            return None
    await notify(f"🚀 Published! KDP is reviewing it (up to 72 hours). I'll check twice a day and send the link.")
    return "in_review"


@stage_worker("in_review")
async def review_status_worker(book: dict) -> str | None:
    from .pipeline import book_path
    from .telegram_bot import notify
    slug = book["slug"]
    lst = settings.read_json(book_path(slug, "listing.json"), {})
    per_day = settings.load_config()["after_launch"]["review_checks_per_day"]
    status = await bookshelf_status(lst.get("title", book["title"]))
    formats = list(jobs.load_book(slug).get("kdp", {}))
    jobs.update_book(slug, kdp_status=status)
    if any(status.get(f) in ("BLOCKED", "ACTION REQUIRED") for f in formats):
        raise NeedsHuman(f"KDP needs action on this book: {status}. Please check your Bookshelf.")
    if formats and all(status.get(f) == "LIVE" for f in formats):
        links = status.get("links") or []
        jobs.update_book(slug, live_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), amazon_links=links)
        await notify(f"🎉 '{lst.get('title')}' is LIVE on Amazon!\n" + ("\n".join(links) or "(link not shown yet)"))
        return "live"
    jobs.snooze(slug, 24 * 3600 / per_day)
    return None

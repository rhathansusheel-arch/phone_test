"""The 7 safety tests (Phase 7). Each returns (name, passed, details).

They run inside a throw-away copy of the agent's folders (never your real books or data)
and use the pretend KDP website from tests/fake_kdp.py, so nothing touches your real KDP.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from . import ads, jobs, limits, niches, settings, state
from .errors import NeedsHuman, UsageLimit  # noqa: F401
from .kdp_upload import LockedField, check_live_changes

ROOT_CODE = Path(__file__).resolve().parent.parent


class Recorder:
    """Stands in for Telegram during the tests: records what would be sent."""

    def __init__(self, answers: dict[str, str]):
        self.answers, self.asked, self.said, self.photos = answers, [], [], []

    async def ask_me(self, text, buttons=None, photos=None, typed_for=None, about=None, timeout=None):
        self.asked.append({"about": about, "text": text, "photos": [str(p) for p in photos or []]})
        key = (about or "").split(":")[0]
        return {"choice": self.answers.get(key, (buttons or ["OK"])[-1]), "text": None}

    async def notify(self, text):
        self.said.append(text)

    async def send_photos(self, paths):
        self.photos += [str(p) for p in paths]


def _install(rec: Recorder) -> None:
    from . import telegram_bot as tb
    tb.ask_me, tb.notify, tb.send_photos = rec.ask_me, rec.notify, rec.send_photos


def _make_listed_book(slug: str = "safety-book") -> str:
    """A small, already-built book (interior + cover PDFs) ready for upload."""
    from pypdf import PdfWriter
    d = jobs.book_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    for fmt in ("paperback",):
        for kind in ("interior", "cover"):
            w = PdfWriter()
            w.add_blank_page(612, 792)
            with open(d / f"{kind}_{fmt}.pdf", "wb") as f:
                w.write(f)
    jobs.create_book("Safety Test Coloring Book", slug=slug, stage="listed")
    settings.write_json(d / "plan.json", {"source": "inbox", "theme": "test", "audience": "kids", "ages": [4, 8],
                                          "seed": "test", "formats": ["paperback"], "cover_finish": "matte"})
    settings.write_json(d / "build.json", {"formats": {"paperback": {"trim": "8.5x11", "pages": 30,
                                                                       "path": str(d / "interior_paperback.pdf")}}})
    settings.write_json(d / "listing.json", {
        "title": "Safety Test Coloring Book", "subtitle": "A Pretend Book for Testing",
        "description": "Test.\nInside you'll find:\n• 1\n• 2\n• 3\n• 4\n• 5",
        "keywords": ["one", "two", "three", "four", "five", "six", "seven"],
        "categories": ["Juvenile Nonfiction > Activity Books > Coloring"] * 3, "reading_age": {"min": 4, "max": 8},
        "prices": {"paperback": {"US": 8.99, "royalty_usd": 1.66, "printing_cost": 2.84}}})
    settings.write_json(settings.DATA / "kdp_specs.json", {"verified_from_kdp": True})
    settings.write_json(settings.DATA / "answers.json", {"low_content": False})
    return slug


# ------------------------------------------------------------ the 7 tests

async def t_weekly_limit() -> tuple[str, bool, str]:
    from .kdp_upload import fill_format
    slug = _make_listed_book("limit-book")
    limits.use_slot("paperback", "created", "earlier-book-1")
    limits.use_slot("paperback", "created", "earlier-book-2")
    refused = False
    try:
        limits.use_slot("paperback", "created", "third-book")
    except limits.NoSlotLeft:
        refused = True
    result = await fill_format(slug, "paperback")    # must queue without opening KDP
    ok = refused and result.get("queued") is True
    return ("Weekly limit: a 3rd paperback in one week is refused and queued", ok,
            f"refused={refused}, uploader said: {result.get('why')}")


async def t_ai_disclosure(fake) -> tuple[str, bool, str]:
    from .kdp_upload import fill_format
    slug = _make_listed_book("ai-book")
    rec = Recorder({"categories": "Done"})
    _install(rec)
    r = await fill_format(slug, "paperback")
    record = next(iter(fake.books.values()))["formats"]["paperback"]
    ok = (record.get("ai") == "yes" and "Claude" in record.get("ai_images_tool", "")
          and record.get("status") == "DRAFT" and not any("publish" in c for c in fake.clicks))
    return ("AI disclosure: the draft's AI answer is Yes (images: Claude by Anthropic)", ok,
            f"AI answer={record.get('ai')}, images={record.get('ai_images')}, tool={record.get('ai_images_tool')}, "
            f"saved as {record.get('status')}, publish clicked={any('publish' in c for c in fake.clicks)}")


async def t_trademark() -> tuple[str, bool, str]:
    problem = niches.trademark_problem("bluey coloring book for kids")
    seeds_ok = not any("bluey" in s["seed"] for s in niches.seeds())
    blocked = False
    try:
        niches.start_book({"theme": "Bluey", "audience": "kids", "_seed": {"seed": "bluey coloring book", "audience": "kids"}},
                          {"summary": {}})
    except ValueError:
        blocked = True
    from .listing import validate
    lst = {"title": "Bluey Coloring Book", "subtitle": "", "description": "Inside you'll find:\n• a\n• b\n• c\n• d\n• e",
           "keywords": ["a1", "b2", "c3", "d4", "e5", "f6", "g7"], "categories": ["a", "b", "c"]}
    listing_blocked = any("Trademark" in p for p in validate(lst, {"picture_count": 1})[0])
    ok = bool(problem) and seeds_ok and blocked and listing_blocked
    return ("Trademark filter: a niche containing 'Bluey' is rejected", ok,
            f"niche check: {problem}; book creation blocked={blocked}; listing blocked={listing_blocked}")


async def t_login_stop(fake) -> tuple[str, bool, str]:
    from .kdp_upload import bookshelf_status
    results = []
    for mode in ("login", "captcha"):
        fake.mode = mode
        rec = Recorder({"login-stop": "Stop this job"})
        _install(rec)
        try:
            await bookshelf_status("anything")
            results.append((mode, False, "did not stop"))
        except NeedsHuman:
            sent_photo = bool(rec.asked and rec.asked[0]["photos"])
            results.append((mode, bool(rec.asked) and sent_photo, "paused and messaged you with a screenshot"))
    fake.mode = "normal"
    ok = all(r[1] for r in results)
    return ("Login stop: a login or CAPTCHA page pauses the agent and messages you", ok,
            "; ".join(f"{m}: {d}" for m, _, d in results))


async def t_locked_fields() -> tuple[str, bool, str]:
    jobs.create_book("Live Test Book", slug="live-book", stage="live")
    try:
        check_live_changes("live-book", {"title": "A New Title"})
        return ("Locked fields: changing a live book's title is refused", False, "title change was allowed!")
    except LockedField as exc:
        msg = str(exc)
    allowed = True
    try:
        check_live_changes("live-book", {"keywords": ["x"] * 7, "price": {"paperback": 9.99}})
    except LockedField:
        allowed = False
    return ("Locked fields: changing a live book's title is refused", allowed,
            f"refused with: {msg}; keyword/price change still allowed={allowed}")


async def t_ads() -> tuple[str, bool, str]:
    from . import sales
    jobs.create_book("Ads Test Book", slug="ads-book", stage="live")
    sales.import_rows([{"date": "2026-11-01", "slug": "ads-book", "title": "Ads Test Book", "asin": "X",
                        "marketplace": "Amazon.com", "format": "Paperback", "units": 10, "royalty": 16.6,
                        "currency": "USD", "royalty_inr": 1394.4}])
    plan = ads.plan_for("ads-book")
    rec = Recorder({})
    _install(rec)
    await ads.daily_check()                     # must not ask to create anything
    msg = await ads.stop_all()
    ok = plan["action"] == "nothing" and not rec.asked and state.load().get("ads_stopped") is True
    state.update(ads_stopped=False)
    return ("Ads: below ₹2,000 profit no campaign is created; /stopads works", ok,
            f"profit ₹{plan['profit_inr']:.0f} → {plan['action']} ({plan['why']}); /stopads said: {msg[:80]}")


async def t_restart() -> tuple[str, bool, str]:
    """Start a real agent process, kill it mid-book, start again: it must continue from the saved stage."""
    script = textwrap.dedent(f"""
        import asyncio, sys, os
        sys.path.insert(0, {str(ROOT_CODE)!r})
        from kdp_agent import jobs, state
        slow = sys.argv[1] == "slow"
        @jobs.stage_worker("planned")
        async def a(b): return "drawing"
        @jobs.stage_worker("drawing")
        async def b(b):
            if slow:
                await asyncio.sleep(120)   # killed in the middle of this
            return "built"
        async def main():
            for _ in range(3):
                await jobs.run_once()
        asyncio.run(main())
    """)
    path = settings.DATA / "_restart_test.py"
    path.write_text(script, encoding="utf-8")
    jobs.create_book("Restart Test", slug="restart-book")
    env = dict(os.environ, KDP_AGENT_ROOT=str(settings.ROOT))
    proc = subprocess.Popen([sys.executable, str(path), "slow"], env=env)
    for _ in range(100):
        time.sleep(0.2)
        if jobs.load_book("restart-book")["stage"] == "drawing":
            break
    time.sleep(1)
    proc.kill()                                   # "the PC crashed" in the middle of drawing
    proc.wait()
    stage_after_crash = jobs.load_book("restart-book")["stage"]
    subprocess.run([sys.executable, str(path), "fast"], env=env, timeout=120)
    final = jobs.load_book("restart-book")
    history = [h["stage"] for h in final["history"]]
    ok = stage_after_crash == "drawing" and final["stage"] == "built" and history == ["planned", "drawing", "built"]
    return ("Restart: stopped mid-book, it continues from the saved stage", ok,
            f"stage after crash={stage_after_crash}, after restart={final['stage']}, history={history}")


async def run_all() -> list[tuple[str, bool, str]]:
    """Must be called with KDP_AGENT_ROOT pointing at a throw-away folder."""
    sys.path.insert(0, str(ROOT_CODE / "tests"))
    from fake_kdp import FakeKdp
    fake = FakeKdp(port=8798).start()
    cfg = (settings.ROOT / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace('kdp_base_url: "https://kdp.amazon.com/en_US"', f'kdp_base_url: "{fake.base}"')
    cfg = cfg.replace("min_pause_seconds: 2", "min_pause_seconds: 0.05").replace("max_pause_seconds: 5", "max_pause_seconds: 0.1")
    cfg = cfg.replace("show_kdp_window: true", "show_kdp_window: false")
    (settings.ROOT / "config.yaml").write_text(cfg, encoding="utf-8")
    settings.ensure_folders()
    results = []
    tests = [("Weekly limit", t_weekly_limit), ("AI disclosure", lambda: t_ai_disclosure(fake)),
             ("Trademark filter", t_trademark), ("Login stop", lambda: t_login_stop(fake)),
             ("Locked fields", t_locked_fields), ("Ads", t_ads), ("Restart", t_restart)]
    for name, t in tests:
        (settings.DATA / "limits.json").unlink(missing_ok=True)    # each test starts with a fresh week
        try:
            results.append(await t())
        except Exception as exc:
            results.append((name, False, f"crashed: {type(exc).__name__}: {exc}"))
    fake.stop()
    return results


def checklist(results) -> str:
    lines = ["🛡 Safety tests:"]
    for name, ok, detail in results:
        lines.append(f"{'✅' if ok else '❌'} {name}\n    {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    lines.append(f"\n{passed}/{len(results)} passed.")
    return "\n".join(lines)

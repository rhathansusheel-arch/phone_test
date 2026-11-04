"""Tests for Phases 2-7 that need no internet, no Claude and no KDP account."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kdp_agent import (ads, after_launch, artist, covers, interior, jobs, listing, niches, pdftools, sales,
                       settings, specs, state, taste)
from kdp_agent.safety_tests import Recorder, _install

import samples


# ---------------------------------------------------------------- Phase 2: KDP numbers

def test_spine_cover_and_costs():
    size = specs.paperback_cover_size("8.5x11", 86)
    assert size["spine"] == pytest.approx(86 * 0.002252, abs=1e-4)
    assert size["width"] == pytest.approx(0.125 + 8.5 + size["spine"] + 8.5 + 0.125, abs=1e-3)
    assert size["height"] == pytest.approx(11.25)
    assert size["spine_text"] is True
    assert specs.paperback_cover_size("8.5x11", 78)["spine_text"] is False
    assert specs.gutter(86) == 0.375 and specs.gutter(200) == 0.5
    assert specs.printing_cost("paperback", "8.5x11", 86) == 2.84
    assert specs.printing_cost("paperback", "6x9", 200) == pytest.approx(1.00 + 0.012 * 200)
    assert specs.royalty(9.99, 2.84) == pytest.approx(0.6 * 9.99 - 2.84, abs=0.01)
    assert specs.royalty(8.99, 2.84) == pytest.approx(0.5 * 8.99 - 2.84, abs=0.01)
    assert specs.page_count_ok("hardcover", "8.25x11", 60)
    assert specs.page_count_ok("hardcover", "8.5x11", 100)       # not a hardcover size
    assert specs.page_count_ok("paperback", "8.5x11", 86) is None
    assert specs.nearest_trim(8.5, 11, specs.hardcover_trims()) == "8.25x11"


def test_price_options_match_competitors_and_stay_above_minimum():
    p = listing.price_options("paperback", "8.5x11", 86, [6.99, 7.99, 8.99, 7.49])
    prices = [o["price"] for o in p["options"]]
    assert len(prices) == 2 and all(x >= specs.min_price(2.84) for x in prices)
    assert all(o["royalty"] > 0 for o in p["options"])
    cheap = listing.price_options("paperback", "8.5x11", 86, [2.99, 3.49])
    assert all(o["royalty"] > 0 for o in cheap["options"])          # never below KDP's minimum


GOOD = {"title": "Ocean Coloring Book for Toddlers", "subtitle": "Big Simple Sea Animals for Kids Ages 2-5",
        "description": "Little hands, big fun!\nA colouring book.\nInside you'll find:\n• 40 pictures\n• 2\n• 3\n• 4\n• 5",
        "keywords": ["preschool colouring pages", "underwater creatures", "whale pictures", "crayon fun 2 year old",
                     "fine motor practice", "quiet time activity", "beach trip gift"],
        "categories": ["Juvenile Nonfiction > Activity Books > Coloring", "Juvenile Nonfiction > Animals > Marine Life",
                       "Juvenile Nonfiction > Animals > Fishes"], "blurb": "Fun."}
FACTS = {"picture_count": 40, "subjects": ["whale", "crab"]}


def test_listing_rules():
    assert listing.validate(GOOD, FACTS)[0] == []
    bad = dict(GOOD, title="Best New Ocean Coloring Book 2026", keywords=GOOD["keywords"][:6] + ["ocean, toddlers"])
    problems = listing.validate(bad, FACTS)[0]
    text = " ".join(problems)
    assert "banned words" in text and "comma" in text and "repeats words" in text
    wrong_count = dict(GOOD, description=GOOD["description"].replace("40 pictures", "50 pictures"))
    assert any("50 pictures" in p for p in listing.validate(wrong_count, FACTS)[0])
    medical = dict(GOOD, description=GOOD["description"] + "\nCures anxiety.")
    assert any("medical" in p for p in listing.validate(medical, FACTS)[0])
    fake_subject = dict(GOOD, keywords=GOOD["keywords"][:6] + ["shark pictures"])
    assert any("shark" in p for p in listing.validate(fake_subject, FACTS)[0])
    assert any("large print" in p for p in listing.validate(dict(GOOD, subtitle="Large Print Sea Animals"), FACTS)[0])


# ---------------------------------------------------------------- Phase 2: checker, interior, covers

@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "ocean.pdf"
    asyncio.run(samples.make(path))
    return path


def test_checker_finds_every_planted_problem(sample_pdf):
    rep = pdftools.check(sample_pdf)
    text = rep.text_summary()
    assert len(rep.pictures) == 6
    assert "blank back" in text and "Grey" in text and "thin lines" in text
    assert "Bookmarks" in text and "Metadata" in text and "margins" in text


async def test_interior_and_cover_are_kdp_ready(sample_pdf, tmp_path):
    rep = pdftools.check(sample_pdf)
    pics = [p for p in interior.pictures_from_report(rep)][:3] * 4    # 12 clean-ish pictures -> 30 pages
    pics = [p for p in pics if rep.page_info[p.index].grey == 0 and rep.page_info[p.index].thin < 0.25]
    res = await interior.build_interior("Ocean Friends", "For Kids", pics, "8.5x11", tmp_path / "int.pdf")
    final = pdftools.check(res["path"], final=True)
    assert not final.needs_you(), final.text_summary()
    assert res["picture_pages"] == list(range(5, 5 + 2 * len(pics), 2))
    png = pdftools.crop_picture_png(res["path"], 4, 150, tmp_path / "p.png")
    cov = await covers.make_cover("paperback", "8.5x11", res["pages"], tmp_path / "cover.pdf", "Ocean Friends",
                                  "For Kids Ages 2-5", "Rhaths", "Ages 2-5", "Blurb", [png])
    assert all(c["ok"] is not False for c in cov["checks"]), cov["checks"]


# ---------------------------------------------------------------- Phase 4: artist checks

GOOD_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1333"><circle cx="500" cy="666" r="330" '
            'fill="none" stroke="black" stroke-width="16"/><circle cx="420" cy="600" r="30" fill="black"/></svg>')


def test_svg_code_rules():
    assert artist.static_problems(GOOD_SVG) == []
    assert artist.static_problems(GOOD_SVG.replace('fill="none"', 'fill="#ff0000"'))
    assert artist.static_problems(GOOD_SVG.replace("</svg>", "<text>Hi</text></svg>"))
    assert artist.static_problems(GOOD_SVG.replace('fill="none"', 'fill="none" opacity="0.5"'))
    assert artist.static_problems("<svg><circle></svg>")


async def test_page_pixel_checks(tmp_path):
    from PIL import Image
    ok = await artist.render_svg(GOOD_SVG, tmp_path / "ok.png", 750, 1000, crisp=True)
    problems, info = artist.pixel_problems(Image.open(ok), 100, "toddler", "x", 1)
    assert problems == [], problems
    edge = GOOD_SVG.replace('r="330"', 'r="660"')
    p2, _ = artist.pixel_problems(Image.open(await artist.render_svg(edge, tmp_path / "e.png", 750, 1000, True)),
                                  100, "toddler", "x", 1)
    assert any("edge" in p for p in p2)
    thin = GOOD_SVG.replace('stroke-width="16"', 'stroke-width="1"')
    p3, _ = artist.pixel_problems(Image.open(await artist.render_svg(thin, tmp_path / "t.png", 750, 1000, True)),
                                  100, "toddler", "x", 1)
    assert any("thin" in p or "Too little" in p for p in p3)


# ---------------------------------------------------------------- Phase 5: niches + taste

def fake_research_data(seed, bsr=40000, reviews=60):
    return {"seed": seed, "fetched_at": datetime.now(timezone.utc).isoformat(), "suggestions": [seed + " for kids"],
            "top": [{"title": "x", "price": 7.99, "reviews": reviews, "bsr": bsr, "pages": 90, "trim": "8.5x11"}] * 3,
            "summary": {"median_price": 7.99, "median_reviews": reviews, "median_bsr": bsr, "median_pages": 90,
                        "common_trim": "8.5x11", "prices": [7.99] * 3, "count": 3}}


def test_seasonal_seeds_and_trademarks():
    names = " ".join(s["seed"] for s in niches.seeds(date(2026, 11, 4)))
    assert "christmas" in names and "halloween" not in names         # 6-10 weeks ahead only
    assert niches.trademark_problem("Bluey coloring book")
    assert niches.trademark_problem("ocean coloring book") is None


async def test_research_round_level1_and_level2(monkeypatch):
    async def fake(seed, words=None, **k):
        return fake_research_data(seed, bsr=20000 if "dinosaur" in seed else 200000)
    monkeypatch.setattr(niches.research, "research", fake)
    rec = Recorder({"niche": "1"})
    _install(rec)
    rows = await niches.research_round(force=True)
    assert rows and rec.asked and rec.asked[0]["about"] == "niche"
    assert "1." in rec.asked[0]["text"] and "Top 2:" in rec.asked[0]["text"]
    assert len(jobs.all_books()) == 1 and jobs.all_books()[0]["stage"] == "planned"
    assert taste.load()["approval_streak"] == 1
    (settings.DATA / "research" / "_last_round.json").unlink()
    state.update(level=2)
    rec2 = Recorder({})
    _install(rec2)
    await niches.research_round(force=True)
    assert not rec2.asked and any("Level 2" in s for s in rec2.said)   # picked by itself, told you why
    assert (settings.DATA / "niches.csv").exists()
    state.update(level=1)


def test_level2_offer_after_5_approvals_in_a_row():
    for i in range(5):
        taste.record("niche", "skipped", f"idea b{i}")
        taste.record("niche", "approved", f"idea a{i}", changed=False)
    assert taste.load()["approval_streak"] == 5
    taste.record("niche", "rejected", "idea x", reason="no")
    assert taste.load()["approval_streak"] == 0


def test_taste_learning():
    taste.record("niche", "rejected", "halloween coloring book", reason="too scary")
    taste.record("niche", "approved", "farm animals coloring book")
    assert taste.word_score("farm coloring") > taste.word_score("halloween coloring")
    assert "too scary" in taste.summary()


# ---------------------------------------------------------------- Phase 6: sales, report, ads, slow sellers

def _report(tmp_path) -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Royalty report"])
    ws.append(["Royalty Date", "Title", "ASIN/ISBN", "Marketplace", "Transaction Type", "Units Sold", "Royalty", "Currency"])
    ws.append(["2026-11-02", "Ocean Book", "B0X", "Amazon.com", "Paperback", 3, 4.98, "USD"])
    ws.append(["2026-11-03", "Ocean Book", "B0X", "Amazon.co.uk", "Paperback", 1, 1.20, "GBP"])
    path = tmp_path / "report.xlsx"
    wb.save(path)
    return path


def test_sales_report_and_weekly_report(tmp_path):
    jobs.create_book("Ocean Book", slug="ocean", stage="live")
    rows = sales.parse_report(_report(tmp_path))
    assert len(rows) == 2 and rows[0]["slug"] == "ocean"
    sales.import_rows(rows)
    sales.import_rows(rows)                      # importing twice doesn't double count
    assert sales.units("ocean") == 4
    assert sales.royalties_inr("ocean") == pytest.approx(4.98 * 84 + 1.20 * 110)
    report = sales.weekly_report(date(2026, 11, 8))
    assert "Ocean Book" in report and "slots" in report


def test_ads_rule():
    jobs.create_book("Ocean Book", slug="ocean", stage="live")
    sales.import_rows([{"date": "2026-11-01", "slug": "ocean", "title": "Ocean Book", "asin": "", "marketplace": "Amazon.com",
                        "format": "", "units": 20, "royalty": 30, "currency": "USD", "royalty_inr": 2520}])
    plan = ads.plan_for("ocean")
    assert plan["action"] == "create campaign"
    assert plan["max_total_spend_inr"] == pytest.approx(0.3 * 2520)
    assert plan["daily_budget_inr"] <= settings.load_config()["ads"]["daily_cap_inr"]
    ads.record("ocean", "camp", 700, 0, "running")
    assert ads.profit_inr("ocean") == pytest.approx(2520 - 700)
    assert ads.should_pause("ocean", "camp", 700, 0)          # used its share with no sales
    state.update(ads_stopped=True)
    assert ads.plan_for("ocean")["action"] == "nothing"
    state.update(ads_stopped=False)


def test_slow_seller_detection_and_backup():
    jobs.create_book("Slow Book", slug="slow", stage="live",
                     live_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    jobs.create_book("New Book", slug="new", stage="live", live_at=datetime.now(timezone.utc).isoformat())
    assert [b["slug"] for b in after_launch.slow_sellers()] == ["slow"]
    settings.write_json(jobs.book_dir("slow") / "listing.json", {"title": "Slow Book"})
    path = Path(after_launch.backup())
    assert (path / "books" / "slow" / "listing.json").exists() and (path / "data").exists()


# ---------------------------------------------------------------- Phase 7: timers never block books

async def test_timer_waiting_for_you_does_not_block_books():
    jobs.WORKERS.clear()
    jobs.create_book("Quick", slug="quick")

    @jobs.stage_worker("planned")
    async def w(book):
        return "drawing"

    async def slow_timer():
        await asyncio.sleep(30)     # like a question waiting for your tap
    jobs.PERIODIC[:] = [slow_timer]
    task = asyncio.create_task(jobs.run_forever(0.05))
    await asyncio.sleep(0.5)
    task.cancel()
    jobs.PERIODIC[:] = []
    for t in list(jobs._running.values()):
        t.cancel()
    assert jobs.load_book("quick")["stage"] == "drawing"
    jobs.WORKERS.clear()

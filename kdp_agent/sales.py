"""Sales: read KDP Reports once a day into data/sales.csv, weekly report, what sells.

Two ways in:
1. The agent opens KDP Reports with your KDP session and downloads the royalties report.
2. You can drop any KDP report file (.xlsx or .csv) into inbox/reports - the agent reads it.
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import jobs, limits, settings

log = logging.getLogger("sales")

FIELDS = ["date", "slug", "title", "asin", "marketplace", "format", "units", "royalty", "currency", "royalty_inr"]
REPORTS_URL = "https://kdpreports.amazon.com/dashboard"

COLUMN_NAMES = {
    "date": ["royalty date", "order date", "date", "invoice date"],
    "title": ["title"],
    "asin": ["asin", "isbn", "asin/isbn"],
    "marketplace": ["marketplace", "store"],
    "format": ["transaction type", "format", "binding"],
    "units": ["net units sold", "units sold", "paid units", "net units", "units"],
    "royalty": ["royalty", "royalty amount", "net royalty"],
    "currency": ["currency"],
}
MARKET_CURRENCY = {"amazon.com": "USD", "amazon.co.uk": "GBP", "amazon.de": "EUR", "amazon.fr": "EUR",
                   "amazon.es": "EUR", "amazon.it": "EUR", "amazon.nl": "EUR", "amazon.ca": "CAD",
                   "amazon.com.au": "AUD", "amazon.co.jp": "JPY", "amazon.in": "INR", "amazon.pl": "EUR",
                   "amazon.se": "EUR"}


def sales_path() -> Path:
    return settings.DATA / "sales.csv"


def load() -> list[dict]:
    try:
        with open(sales_path(), newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    for r in rows:
        r["units"] = int(float(r["units"] or 0))
        r["royalty"] = float(r["royalty"] or 0)
        r["royalty_inr"] = float(r["royalty_inr"] or 0)
    return rows


def save(rows: list[dict]) -> None:
    sales_path().parent.mkdir(parents=True, exist_ok=True)
    with open(sales_path(), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in FIELDS})


def to_inr(amount: float, currency: str) -> float:
    rates = settings.load_config()["money"]["to_inr"]
    return round(amount * float(rates.get(currency.upper(), rates["USD"])), 2)


def _title_to_slug() -> dict[str, str]:
    out = {}
    for b in jobs.all_books():
        lst = settings.read_json(jobs.book_dir(b["slug"]) / "listing.json", {})
        for t in (b.get("title"), lst.get("title")):
            if t:
                out[t.strip().lower()] = b["slug"]
    return out


def _find_columns(header: list[str]) -> dict[str, int]:
    cols = {}
    low = [str(h or "").strip().lower() for h in header]
    for key, names in COLUMN_NAMES.items():
        for name in names:
            if name in low:
                cols[key] = low.index(name)
                break
    return cols


def parse_report(path: Path) -> list[dict]:
    """Read a KDP report (.xlsx or .csv). Finds the header row by its column names."""
    tables: list[list[list]] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            tables.append([list(r) for r in ws.iter_rows(values_only=True)])
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            tables.append([r for r in csv.reader(f)])
    titles = _title_to_slug()
    out = []
    for table in tables:
        for i, row in enumerate(table[:30]):
            cols = _find_columns(row)
            if {"title", "units", "royalty"} <= set(cols):
                break
        else:
            continue
        for row in table[i + 1:]:
            if not row or all(c in (None, "") for c in row):
                continue
            get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None
            title = str(get("title") or "").strip()
            if not title:
                continue
            try:
                units = int(float(get("units") or 0))
                royalty = float(re.sub(r"[^\d.\-]", "", str(get("royalty") or 0)) or 0)
            except ValueError:
                continue
            market = str(get("marketplace") or "Amazon.com").strip()
            currency = str(get("currency") or MARKET_CURRENCY.get(market.lower(), "USD")).strip()
            d = get("date")
            d = d.date().isoformat() if isinstance(d, datetime) else str(d or date.today().isoformat())[:10]
            out.append({"date": d, "slug": titles.get(title.lower(), ""), "title": title,
                        "asin": str(get("asin") or ""), "marketplace": market, "format": str(get("format") or ""),
                        "units": units, "royalty": royalty, "currency": currency,
                        "royalty_inr": to_inr(royalty, currency)})
    return out


def import_rows(new_rows: list[dict]) -> int:
    """Add rows; a row for the same day/book/marketplace/format replaces the old one."""
    key = lambda r: (r["date"], r["title"].lower(), r["marketplace"], r["format"])
    rows = {key(r): r for r in load()}
    for r in new_rows:
        rows[key(r)] = r
    save(sorted(rows.values(), key=lambda r: (r["date"], r["title"])))
    return len(new_rows)


def import_dropped_files() -> int:
    folder = settings.INBOX / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    done = folder / "done"
    count = 0
    for f in list(folder.glob("*.xlsx")) + list(folder.glob("*.csv")):
        count += import_rows(parse_report(f))
        done.mkdir(exist_ok=True)
        shutil.move(str(f), done / f.name)
    return count


async def read_kdp_reports() -> int:
    """Open KDP Reports with your KDP session and download the royalties report."""
    from .browser import goto, human_pause, profile_session
    from .kdp_upload import Form
    count = import_dropped_files()
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await goto(page, REPORTS_URL, "opening KDP Reports")
        form = Form(page, "_reports", "reports")
        for name in (["Royalties", "Orders"]):
            await form.click([name], required=False)
        button = await form.button(["Download Report", "Download", "Generate Report", "Export"], required=False)
        if button is None:
            await form.stop("I couldn't find the download button on KDP Reports. You can drop a report file "
                            "into inbox\\reports instead")
        async with page.expect_download(timeout=120_000) as dl:
            await button.click()
        download = await dl.value
        target = settings.DATA / "reports" / (datetime.now().strftime("%Y%m%d-%H%M") + "-" + download.suggested_filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(target))
        await human_pause()
    count += import_rows(parse_report(target))
    return count


# ------------------------------------------------------------ numbers

def per_book(since: date | None = None) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"units": 0, "royalty_inr": 0.0, "title": ""})
    for r in load():
        if since and r["date"] < since.isoformat():
            continue
        k = r["slug"] or r["title"]
        out[k]["units"] += r["units"]
        out[k]["royalty_inr"] += r["royalty_inr"]
        out[k]["title"] = r["title"]
    return dict(out)


def royalties_inr(slug: str) -> float:
    return round(sum(r["royalty_inr"] for r in load() if r["slug"] == slug), 2)


def units(slug: str, since: date | None = None) -> int:
    return sum(r["units"] for r in load() if r["slug"] == slug and (not since or r["date"] >= since.isoformat()))


def best_theme_words(n: int = 5) -> list[str]:
    """Theme words from books that sell best (used to seed new niches)."""
    from .niches import _words
    score: dict[str, float] = defaultdict(float)
    for k, v in per_book().items():
        if v["units"] <= 0:
            continue
        for w in _words(v["title"]):
            score[w] += v["units"]
    return [w for w, _ in sorted(score.items(), key=lambda x: -x[1])[:n]]


def theme_fit(text: str) -> float:
    """0..1: how much this niche looks like books that sell."""
    from .niches import _words
    totals = per_book()
    all_units = sum(v["units"] for v in totals.values())
    if all_units <= 0:
        return 0.5   # no sales data yet - neutral
    mine = _words(text)
    hit = sum(v["units"] for v in totals.values() if mine & _words(v["title"]))
    return round(hit / all_units, 3)


def what_sells() -> str:
    """Short summary for /taste: which themes, ages, sizes and prices sell."""
    totals = per_book()
    if not any(v["units"] for v in totals.values()):
        return "No sales yet, so I can't tell what sells."
    lines = []
    for k, v in sorted(totals.items(), key=lambda x: -x[1]["units"])[:5]:
        plan = settings.read_json(jobs.book_dir(k) / "plan.json", {}) if (settings.BOOKS / k).exists() else {}
        lst = settings.read_json(jobs.book_dir(k) / "listing.json", {}) if (settings.BOOKS / k).exists() else {}
        price = lst.get("prices", {}).get("paperback", {}).get("US")
        lines.append(f"• {v['title'][:50]}: {v['units']} sold, ₹{v['royalty_inr']:.0f} "
                     f"(ages {plan.get('ages') or 'adult'}, {plan.get('trim', '?')}, ${price})")
    return "Best sellers:\n" + "\n".join(lines)


def weekly_report(today: date | None = None) -> str:
    from . import ads
    today = today or date.today()
    week_ago = today - timedelta(days=7)
    week = per_book(week_ago)
    total = per_book()
    live = [b for b in jobs.all_books() if b["stage"] == "live"]
    lines = [f"📊 Weekly report ({week_ago.isoformat()} to {today.isoformat()})",
             f"Books live: {len(live)}"]
    if total:
        lines.append("\nPer book (this week / total):")
        for k, v in sorted(total.items(), key=lambda x: -x[1]["units"]):
            w = week.get(k, {"units": 0, "royalty_inr": 0})
            lines.append(f"• {v['title'][:45]}: {w['units']} / {v['units']} sold, "
                         f"₹{w['royalty_inr']:.0f} / ₹{v['royalty_inr']:.0f}")
        ranked = sorted(total.items(), key=lambda x: -x[1]["units"])
        lines.append(f"\n🏆 Best: {ranked[0][1]['title'][:50]}")
        if len(ranked) > 1:
            lines.append(f"🐢 Slowest: {ranked[-1][1]['title'][:50]}")
    else:
        lines.append("No sales recorded yet.")
    spend = ads.total_spend_inr(since=week_ago)
    week_roy = sum(v["royalty_inr"] for v in week.values())
    lines.append(f"\n📣 Ads this week: spent ₹{spend:.0f}, royalties ₹{week_roy:.0f}"
                 + (" (ads are off)" if not settings.load_config()["ads"]["enabled"] else ""))
    lines.append("\n🗓 KDP slots this week:\n" + limits.summary())
    coming = [b for b in jobs.all_books() if b["stage"] in ("planned", "drawing", "built", "listed", "drafted",
                                                            "waiting_approval")]
    lines.append("\n📚 Coming next: " + (", ".join(f"{b['title'][:35]} ({b['stage']})" for b in coming) or "nothing yet"))
    return "\n".join(lines)

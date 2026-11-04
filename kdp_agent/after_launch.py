"""After-launch timers: daily sales, Sunday report, slow-seller fixes, ads, backups.

Each job remembers when it last ran (data/timers.json), so restarts don't repeat work.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, timedelta, timezone

from . import ads, jobs, listing, research, sales, settings, taste
from .errors import UsageLimit

log = logging.getLogger("after_launch")
IST = timezone(timedelta(hours=5, minutes=30))


def _timers() -> dict:
    return settings.read_json(settings.DATA / "timers.json", {})


def _mark(name: str, value: str) -> None:
    t = _timers()
    t[name] = value
    settings.write_json(settings.DATA / "timers.json", t)


def now_ist() -> datetime:
    return datetime.now(IST)


# ------------------------------------------------------------ slow sellers

def slow_sellers(today: date | None = None) -> list[dict]:
    cfg = settings.load_config()["after_launch"]
    today = today or date.today()
    out = []
    for b in jobs.all_books():
        if b["stage"] != "live" or not b.get("live_at") or b.get("on_hold"):
            continue
        live_days = (today - datetime.fromisoformat(b["live_at"]).date()).days
        last_fix = b.get("last_fix_at")
        if last_fix and (today - datetime.fromisoformat(last_fix).date()).days < cfg["slow_seller_days"]:
            continue
        if live_days >= cfg["slow_seller_days"] and sales.units(b["slug"]) < cfg["slow_seller_min_sales"]:
            out.append(b)
    return out


async def propose_fix(slug: str) -> dict | None:
    """Research again and propose new keywords/description/categories/price. Never title, subtitle or author."""
    from .kdp_upload import check_live_changes, update_live
    from .pipeline import book_path, load_plan
    from .telegram_bot import ask_me, notify
    book = jobs.load_book(slug)
    plan = load_plan(slug)
    old = settings.read_json(book_path(slug, "listing.json"), {})
    data = await research.research(plan["seed"], plan.get("audience_words"), force=True)
    feedback = ("This book is live but selling slowly. KDP locks the title and subtitle: keep them EXACTLY as they are: "
                f"title '{old['title']}', subtitle '{old.get('subtitle', '')}'. Improve the 7 keywords, the description "
                "and the 3 categories using the new research, to reach more buyers.")
    new = await listing.write_listing(old.get("facts", {}), research.research_notes(data), feedback=feedback, previous=old)
    new["title"], new["subtitle"] = old["title"], old.get("subtitle", "")
    pb = old.get("prices", {}).get("paperback", {})
    built = settings.read_json(book_path(slug, "build.json"), {})
    trim = built.get("trims", {}).get("paperback", "8.5x11")
    pages = built.get("formats", {}).get("paperback", {}).get("pages", 80)
    opts = listing.price_options("paperback", trim, pages, data["summary"]["prices"])
    new_price = opts["options"][opts["recommended"]]["price"]
    changes = {"description": new["description"], "keywords": new["keywords"], "categories": new["categories"]}
    if pb.get("US") and abs(new_price - pb["US"]) >= 0.5:
        changes["price"] = {"paperback": new_price}
    check_live_changes(slug, changes)   # Hard rule 7 - refuses title/subtitle/author
    diff = [f"🔁 Fix for slow seller '{old['title']}' (live 30+ days, few sales). Title, subtitle and author stay the same.",
            "\nOLD keywords:\n  " + "\n  ".join(old.get("keywords", [])),
            "NEW keywords:\n  " + "\n  ".join(new["keywords"]),
            "\nOLD categories: " + "; ".join(old.get("categories", [])),
            "NEW categories: " + "; ".join(new["categories"])]
    if "price" in changes:
        diff.append(f"\nPrice: ${pb.get('US')} → ${new_price} (competitors' median ${data['summary'].get('median_price')})")
    for chunk in _split("\n".join(diff) + "\n\nNEW description:\n" + new["description"]):
        await notify(chunk)
    a = await ask_me("Apply these changes on KDP?", buttons=["Approve", "No thanks"], typed_for=["No thanks"],
                     about=f"slowfix:{slug}")
    jobs.update_book(slug, last_fix_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not a or a["choice"] != "Approve":
        taste.record("fix", "rejected", old["title"], reason=(a or {}).get("text"))
        return None
    taste.record("fix", "approved", old["title"])
    await update_live(slug, changes)
    await notify(f"✅ Updated '{old['title']}' on KDP (description, keywords, categories"
                 f"{', price' if 'price' in changes else ''}). Updates don't use weekly slots.")
    return changes


def _split(text: str, size: int = 3800) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


# ------------------------------------------------------------ backups

def backup(today: date | None = None) -> str:
    """Copy data\\ and every book's listing.json and status.json to backups\\YYYY-MM-DD."""
    today = today or date.today()
    target = settings.ROOT / "backups" / today.isoformat()
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(settings.DATA, target / "data", ignore=shutil.ignore_patterns("research", "reports"))
    for b in settings.BOOKS.iterdir():
        if not b.is_dir():
            continue
        for name in ("listing.json", "status.json", "plan.json", "pages.json"):
            f = b / name
            if f.exists():
                (target / "books" / b.name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target / "books" / b.name / name)
    keep = settings.load_config()["backups"]["keep_days"]
    for old in (settings.ROOT / "backups").iterdir():
        try:
            if (today - date.fromisoformat(old.name)).days > keep:
                shutil.rmtree(old)
        except ValueError:
            continue
    return str(target)


# ------------------------------------------------------------ the timer job

async def periodic() -> None:
    from .telegram_bot import notify
    cfg = settings.load_config()["after_launch"]
    now = now_ist()
    today = now.date().isoformat()
    t = _timers()

    if t.get("backup") != today:
        path = backup(now.date())
        _mark("backup", today)
        log.info("Backup saved to %s", path)

    any_published = any(b["stage"] in ("in_review", "live") for b in jobs.all_books())
    if any_published and t.get("sales") != today and now.hour >= cfg["sales_check_hour_ist"]:
        try:
            n = await sales.read_kdp_reports()
            log.info("Read %d sales rows", n)
        except UsageLimit:
            raise
        except Exception as exc:
            log.warning("Sales read failed: %s", exc)
            await notify(f"⚠️ I couldn't read KDP Reports today: {exc}")
        _mark("sales", today)

    week = now.strftime("%G-W%V")
    if now.weekday() == 6 and now.hour >= cfg["weekly_report_hour_ist"] and t.get("weekly") != week:
        await notify(sales.weekly_report(now.date()))
        _mark("weekly", week)

    if t.get("slow") != today:
        _mark("slow", today)
        for b in slow_sellers(now.date())[:1]:     # one book a day, to stay gentle
            try:
                await propose_fix(b["slug"])
            except UsageLimit:
                raise
            except Exception as exc:
                log.warning("Slow-seller fix failed for %s: %s", b["slug"], exc)

    if t.get("ads") != today:
        _mark("ads", today)
        await ads.daily_check()

"""Amazon Ads, following the Ads rule in CLAUDE.md exactly.

- A book's profit = its royalties minus its ad spend, in rupees.
- Ads may start only after a book's profit reaches Rs 2,000.
- Then spend at most 30% of that book's profit, never more than the daily cap. Sponsored Products only.
- You approve every new campaign (it spends money). /stopads pauses everything.
- A campaign that uses its budget share with no sales is paused automatically.
Ads stay switched off (config.yaml ads.enabled) until we've tested them together.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import decisions, jobs, sales, settings, state

log = logging.getLogger("ads")

FIELDS = ["date", "slug", "campaign", "spend_inr", "sales_inr", "status"]


def ads_path() -> Path:
    return settings.DATA / "ads.csv"


def load() -> list[dict]:
    try:
        with open(ads_path(), newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    for r in rows:
        r["spend_inr"] = float(r["spend_inr"] or 0)
        r["sales_inr"] = float(r["sales_inr"] or 0)
    return rows


def record(slug: str, campaign: str, spend_inr: float, sales_inr: float, status: str) -> None:
    new = not ads_path().exists()
    ads_path().parent.mkdir(parents=True, exist_ok=True)
    with open(ads_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({"date": date.today().isoformat(), "slug": slug, "campaign": campaign,
                    "spend_inr": spend_inr, "sales_inr": sales_inr, "status": status})


def spend_inr(slug: str) -> float:
    """Total ad spend for a book (each row holds the campaign's running total; take the latest per campaign)."""
    latest: dict[str, float] = {}
    for r in load():
        if r["slug"] == slug:
            latest[r["campaign"]] = r["spend_inr"]
    return round(sum(latest.values()), 2)


def total_spend_inr(since: date | None = None) -> float:
    rows = load()
    by_campaign: dict[str, list[dict]] = {}
    for r in rows:
        by_campaign.setdefault(r["campaign"], []).append(r)
    total = 0.0
    for camp_rows in by_campaign.values():
        camp_rows.sort(key=lambda r: r["date"])
        end = camp_rows[-1]["spend_inr"]
        start = max([r["spend_inr"] for r in camp_rows if since and r["date"] < since.isoformat()] or [0])
        total += end - start
    return round(total, 2)


def profit_inr(slug: str) -> float:
    return round(sales.royalties_inr(slug) - spend_inr(slug), 2)


def campaigns(slug: str) -> dict[str, str]:
    """campaign name -> latest status"""
    out = {}
    for r in load():
        if r["slug"] == slug:
            out[r["campaign"]] = r["status"]
    return out


def plan_for(slug: str) -> dict:
    """What the ads step would do for this book (creates nothing)."""
    cfg = settings.load_config()["ads"]
    royalties = sales.royalties_inr(slug)
    spent = spend_inr(slug)
    profit = round(royalties - spent, 2)
    threshold = float(cfg["start_after_profit_inr"])
    max_total = round(float(cfg["max_share_of_profit"]) * max(profit, 0), 2)
    daily = min(float(cfg["daily_cap_inr"]), round(max_total / 14, 2)) if max_total > 0 else 0
    has = {c: s for c, s in campaigns(slug).items() if s != "paused"}
    if state.load().get("ads_stopped"):
        action, why = "nothing", "ads are stopped (/stopads)"
    elif profit < threshold:
        action, why = "nothing", f"profit ₹{profit:.0f} is below ₹{threshold:.0f}"
    elif has:
        action, why = "keep watching", f"campaign already running ({', '.join(has)})"
    else:
        action, why = "create campaign", f"profit ₹{profit:.0f} reached ₹{threshold:.0f}"
    return {"slug": slug, "royalties_inr": royalties, "spent_inr": spent, "profit_inr": profit,
            "max_total_spend_inr": max_total, "daily_budget_inr": daily, "action": action, "why": why,
            "campaign_type": "Sponsored Products, automatic targeting", "enabled": bool(cfg["enabled"])}


def dry_run_text() -> str:
    live = [b for b in jobs.all_books() if b["stage"] in ("live", "in_review")]
    if not live:
        return "📣 Ads dry run: no live books yet, so nothing would happen."
    lines = ["📣 Ads dry run (nothing is created):"]
    for b in live:
        p = plan_for(b["slug"])
        lines.append(f"• {b['title'][:45]}: royalties ₹{p['royalties_inr']:.0f}, ad spend ₹{p['spent_inr']:.0f}, "
                     f"profit ₹{p['profit_inr']:.0f} → {p['action']} ({p['why']})")
        if p["action"] == "create campaign":
            lines.append(f"   would create: {p['campaign_type']}, daily budget ₹{p['daily_budget_inr']:.0f}, "
                         f"total spend limit ₹{p['max_total_spend_inr']:.0f} (30% of profit)")
    lines.append(f"Ads switched {'ON' if settings.load_config()['ads']['enabled'] else 'OFF'} in config.yaml.")
    return "\n".join(lines)


def allowed_spend(slug: str, spend: float) -> float:
    """Most this book may spend: 30% of its profit (royalties minus this spend)."""
    share = float(settings.load_config()["ads"]["max_share_of_profit"])
    return max(0.0, share * (sales.royalties_inr(slug) - spend))


def should_pause(slug: str, campaign: str, spend: float, sales_inr: float) -> bool:
    """Pause when spend reaches the 30%-of-profit limit, or half of it is used with no sales at all."""
    limit = allowed_spend(slug, spend)
    return spend >= limit or (sales_inr <= 0 and spend >= 0.5 * limit)


async def daily_check() -> None:
    """Start campaigns for books that earned it (after your OK); pause money-losers."""
    from .telegram_bot import ask_me, notify
    cfg = settings.load_config()["ads"]
    if not cfg["enabled"] or state.load().get("ads_stopped"):
        return
    await refresh_spend()
    for b in [b for b in jobs.all_books() if b["stage"] == "live"]:
        p = plan_for(b["slug"])
        if p["action"] != "create campaign":
            continue
        a = await ask_me(f"📣 '{b['title']}' has earned ₹{p['profit_inr']:.0f} profit. Start a Sponsored Products auto "
                         f"campaign with ₹{p['daily_budget_inr']:.0f}/day, total limit ₹{p['max_total_spend_inr']:.0f} "
                         "(30% of profit)? This spends money.", buttons=["Start it", "Not now"], about=f"ads:{b['slug']}")
        if not a or a["choice"] != "Start it":
            continue
        name = await create_campaign(b["slug"], p["daily_budget_inr"])
        record(b["slug"], name, 0, 0, "running")
        decisions.log({"type": "ads_started", "slug": b["slug"], "campaign": name, "daily_inr": p["daily_budget_inr"]})
        await notify(f"📣 Campaign '{name}' started for '{b['title']}'. /stopads pauses all ads at once.")


async def refresh_spend() -> None:
    """Read each running campaign's spend and sales. If they can't be read, pause it (caps can't be checked)."""
    from .browser import goto, profile_session
    from .telegram_bot import notify
    latest = {}
    for r in load():
        latest[(r["slug"], r["campaign"])] = r["status"]
    running = [k for k, s in latest.items() if s == "running"]
    if not running:
        return
    inr = settings.load_config()["money"]["usd_to_inr"]
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await goto(page, "https://advertising.amazon.com/cm/campaigns", "reading Amazon Ads spend")
        headers = [h.strip().lower() for h in await page.get_by_role("columnheader").all_inner_texts()]
        rows = {}
        for slug, camp in running:
            row = page.locator("tr, div[role='row']").filter(has_text=camp)
            if await row.count() and "spend" in headers and "sales" in headers:
                cells = await row.first.get_by_role("cell").all_inner_texts()
                if len(cells) == len(headers):
                    money = lambda t: float(re.sub(r"[^\d.]", "", t) or 0)
                    rows[(slug, camp)] = (money(cells[headers.index("spend")]) * inr,
                                          money(cells[headers.index("sales")]) * inr)
    for slug, camp in running:
        if (slug, camp) not in rows:
            await notify(f"❗ I couldn't read the spend for ad campaign '{camp}', so I can't check its limits. "
                         "Pausing it to be safe.")
            try:
                await _pause_campaign(camp)
                record(slug, camp, spend_inr(slug), 0, "paused")
            except Exception:
                await notify("❗ I couldn't pause it either - please pause it by hand in Amazon Ads.")
            continue
        spend, sales_amt = rows[(slug, camp)]
        status = "running"
        if should_pause(slug, camp, spend, sales_amt):
            await _pause_campaign(camp)
            status = "paused"
            await notify(f"⏸ Paused ad campaign '{camp}': it used its budget share "
                         f"(₹{spend:.0f} spent, ₹{sales_amt:.0f} sales).")
        record(slug, camp, round(spend, 2), round(sales_amt, 2), status)


async def create_campaign(slug: str, daily_budget_inr: float) -> str:
    """Create one Sponsored Products auto campaign from KDP Marketing > Amazon Ads."""
    from .browser import goto, profile_session
    from .kdp_upload import Form, kdp_url
    lst = settings.read_json(jobs.book_dir(slug) / "listing.json", {})
    name = f"{lst.get('title', slug)[:40]} - auto - {date.today().isoformat()}"
    usd = round(daily_budget_inr / settings.load_config()["money"]["usd_to_inr"], 2)
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        form = Form(page, slug, "ads")
        await goto(page, kdp_url("marketing"), "opening KDP Marketing")
        await form.click(["Amazon Ads", "Create an ad campaign", "Create campaign"])
        body = (await form.body_text()).lower()
        if "payment" in body or "billing" in body or "register" in body:
            raise_needs = "Amazon Ads needs account setup or a payment method - please do that part by hand"
            await form.stop(raise_needs)
        await form.click(["Sponsored Products"])
        await form.fill(["Campaign name"], name)
        await form.choose(["Automatic targeting"])
        await form.fill(["Daily budget"], f"{usd:.2f}")
        await form.shot("ads-ready")
        await form.click(["Launch campaign"])
    return name


async def stop_all() -> str:
    """/stopads: no new campaigns, and pause every running campaign."""
    state.update(ads_stopped=True)
    decisions.log({"type": "stopads"})
    latest = {}
    for r in load():
        latest[(r["slug"], r["campaign"])] = r["status"]
    running = [k for k, s in latest.items() if s == "running"]
    if not running:
        return "⏸ Ads stopped. No campaigns were running, and I won't start any until you send /startads."
    paused, failed = [], []
    for slug, camp in running:
        try:
            await _pause_campaign(camp)
            record(slug, camp, spend_inr(slug), 0, "paused")
            paused.append(camp)
        except Exception as exc:
            log.warning("Could not pause %s: %s", camp, exc)
            failed.append(camp)
    msg = f"⏸ Ads stopped. Paused: {', '.join(paused) or 'none'}."
    if failed:
        msg += (f"\n❗ I couldn't pause: {', '.join(failed)}. Please pause them by hand: KDP > Marketing > "
                "Amazon Ads > Campaign manager > switch each campaign off.")
    return msg


async def _pause_campaign(campaign: str) -> None:
    from .browser import goto, profile_session
    from .kdp_upload import Form
    async with profile_session("kdp") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await goto(page, "https://advertising.amazon.com/cm/campaigns", "opening Amazon Ads campaigns")
        row = page.locator("tr, div[role='row']").filter(has_text=campaign)
        if not await row.count():
            raise RuntimeError("campaign row not found")
        toggle = row.first.get_by_role("switch")
        if not await toggle.count():
            raise RuntimeError("pause switch not found")
        if await toggle.first.is_checked():
            await toggle.first.click()
        await Form(page, "_ads", "ads").shot("paused")

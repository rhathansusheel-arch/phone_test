"""Niche research and taste learning.

Runs when fewer than 2 books are getting ready for next week. Seeds come from your taste,
holidays 6-10 weeks ahead, and what sells (sales.csv). Each seed is researched on Amazon
(research profile only), scored, saved to data/niches.csv, and the top 3 are sent to you.
Level 1: you pick. Level 2: the agent picks and tells you why (still asks before publishing).
"""

from __future__ import annotations

import csv
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone

from . import decisions, jobs, research, sales, settings, state, taste
from .errors import UsageLimit
from .safety import find_trademarks

log = logging.getLogger("niches")

NICHES_CSV = "niches.csv"
FIELDS = ["date", "seed", "theme", "audience", "ages", "season", "median_price", "median_reviews", "median_bsr",
          "median_pages", "common_trim", "easy_books", "demand", "competition", "taste", "difference", "sales_fit",
          "season_bonus", "score", "status"]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


DIWALI = {2026: date(2026, 11, 8), 2027: date(2027, 10, 29), 2028: date(2028, 10, 17)}


def holidays(year: int) -> list[tuple[date, str, list[dict]]]:
    """(date, name, seeds). Seeds: phrase + audience."""
    kids = {"audience": "kids", "ages": [4, 8], "audience_words": ["kids ages 4-8", "toddlers", "kids"]}
    tod = {"audience": "toddlers and preschool kids", "ages": [2, 5], "audience_words": ["toddlers", "kids ages 2-5"]}
    adult = {"audience": "adults and seniors", "ages": None, "audience_words": ["adults", "seniors", "women"]}
    out = [
        (date(year, 2, 14), "Valentine's Day", [{"seed": "valentines coloring book for kids", **kids},
                                                 {"seed": "valentines day coloring book for toddlers", **tod}]),
        (date(year, 3, 17), "St Patrick's Day", [{"seed": "st patricks day coloring book for kids", **kids}]),
        (_easter(year), "Easter", [{"seed": "easter coloring book for toddlers", **tod},
                                   {"seed": "easter coloring book for kids", **kids},
                                   {"seed": "bold and easy easter coloring book", **adult}]),
        (_nth_weekday(year, 5, 6, 2), "Mother's Day", [{"seed": "mothers day coloring book for adults", **adult}]),
        (_nth_weekday(year, 6, 6, 3), "Father's Day", [{"seed": "fathers day coloring book for kids", **kids}]),
        (date(year, 6, 21), "Summer", [{"seed": "summer coloring book for kids", **kids},
                                       {"seed": "beach coloring book for toddlers", **tod}]),
        (date(year, 8, 15), "Back to school", [{"seed": "back to school coloring book for kids", **kids}]),
        (date(year, 10, 31), "Halloween", [{"seed": "halloween coloring book for toddlers", **tod},
                                           {"seed": "cute halloween coloring book for kids", **kids},
                                           {"seed": "bold and easy halloween coloring book", **adult}]),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving", [{"seed": "thanksgiving coloring book for kids", **kids}]),
        (date(year, 12, 25), "Christmas", [{"seed": "christmas coloring book for toddlers", **tod},
                                           {"seed": "christmas coloring book for kids", **kids}]),
        (date(year, 12, 31), "New Year / Winter", [{"seed": "winter coloring book for kids", **kids},
                                                   {"seed": "cozy winter coloring book for adults", **adult}]),
    ]
    if year in DIWALI:
        out.append((DIWALI[year], "Diwali", [{"seed": "diwali coloring book for kids", **kids}]))
    return out


EVERGREEN = [
    {"seed": "dinosaur coloring book for kids", "audience": "kids", "ages": [4, 8], "audience_words": ["kids ages 4-8", "boys"]},
    {"seed": "farm animals coloring book for toddlers", "audience": "toddlers and preschool kids", "ages": [2, 5], "audience_words": ["toddlers", "kids ages 1-3"]},
    {"seed": "space coloring book for kids", "audience": "kids", "ages": [4, 8], "audience_words": ["kids ages 4-8"]},
    {"seed": "bugs and insects coloring book for kids", "audience": "kids", "ages": [4, 8], "audience_words": ["kids ages 4-8"]},
    {"seed": "zoo animals coloring book for toddlers", "audience": "toddlers and preschool kids", "ages": [2, 5], "audience_words": ["toddlers"]},
    {"seed": "robot coloring book for kids", "audience": "kids", "ages": [4, 8], "audience_words": ["kids ages 4-8"]},
    {"seed": "bold and easy flowers coloring book", "audience": "adults and seniors", "ages": None, "audience_words": ["adults", "seniors"]},
    {"seed": "cozy cottage coloring book for adults", "audience": "adults and seniors", "ages": None, "audience_words": ["adults", "women"]},
    {"seed": "bold and easy animals coloring book", "audience": "adults and seniors", "ages": None, "audience_words": ["adults", "seniors"]},
    {"seed": "birds coloring book for seniors", "audience": "adults and seniors", "ages": None, "audience_words": ["seniors", "adults"]},
    {"seed": "cute food coloring book for kids", "audience": "kids", "ages": [4, 8], "audience_words": ["kids", "girls"]},
    {"seed": "jungle animals coloring book for toddlers", "audience": "toddlers and preschool kids", "ages": [2, 5], "audience_words": ["toddlers"]},
]


def seeds(today: date | None = None) -> list[dict]:
    """Seed ideas: holidays 6-10 weeks ahead, your taste, what sells, evergreen themes."""
    today = today or date.today()
    lo, hi = settings.load_config()["research"]["seasonal_lead_weeks"]
    out = []
    for year in (today.year, today.year + 1):
        for when, name, items in holidays(year):
            weeks = (when - today).days / 7
            if lo <= weeks <= hi:
                for it in items:
                    out.append({**it, "season": f"{name} ({when.isoformat()})", "season_bonus": 1.0})
    for it in EVERGREEN:
        out.append({**it, "season": "", "season_bonus": 0.0})
    for word in sales.best_theme_words(5):
        out.append({"seed": f"{word} coloring book", "audience": "kids", "ages": [4, 8],
                    "audience_words": ["kids", "toddlers", "adults"], "season": "", "season_bonus": 0.0,
                    "from_sales": True})
    good = []
    seen = set()
    for s in out:
        if s["seed"] in seen:
            continue
        seen.add(s["seed"])
        if trademark_problem(s["seed"]):
            continue
        s["theme"] = re.sub(r"\s*coloring book.*$", "", s["seed"]).strip().title()
        good.append(s)
    return good


def trademark_problem(text: str) -> str | None:
    hits = find_trademarks(text)
    return f"contains a trademark/character ({', '.join(hits)})" if hits else None


def existing_book_words() -> list[set[str]]:
    out = []
    for b in jobs.all_books():
        plan = settings.read_json(jobs.book_dir(b["slug"]) / "plan.json", {})
        text = f"{b['title']} {plan.get('theme', '')} {plan.get('audience', '')}"
        out.append(_words(text))
    return out


def _words(text: str) -> set[str]:
    stop = {"coloring", "colouring", "book", "for", "and", "the", "kids", "ages", "with", "easy", "bold"}
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if w not in stop and len(w) > 2}


def difference(seed: dict) -> float:
    """1 = completely new for you, 0 = same theme AND audience as a book you already have."""
    mine = _words(f"{seed['seed']} {seed['audience']}")
    worst = 0.0
    for other in existing_book_words():
        if mine and other:
            worst = max(worst, len(mine & other) / len(mine | other))
    return round(1 - worst, 3)


def score(seed: dict, data: dict) -> dict:
    s = data["summary"]
    bsr = s.get("median_bsr") or 1_000_000
    reviews = s.get("median_reviews") or 0
    demand = max(0.0, min(1.0, (6 - math.log10(max(bsr, 10))) / 3))
    competition = max(0.0, min(1.0, 1 - math.log10(reviews + 1) / 4))
    easy = sum(1 for r in data["top"] if (r.get("reviews") or 0) < 50 and (r.get("bsr") or 9e9) < 300_000)
    t = taste.word_score(seed["seed"])
    taste_fit = 1 / (1 + math.exp(-t / 2))
    diff = difference(seed)
    sales_fit = sales.theme_fit(seed["seed"])
    total = (0.35 * demand + 0.25 * competition + 0.15 * taste_fit + 0.15 * diff + 0.10 * sales_fit
             + 0.10 * seed.get("season_bonus", 0) + 0.02 * easy)
    if diff < 0.4:
        total -= 0.5   # Hard rule 5: too close to a book you already have
    return {"date": date.today().isoformat(), "seed": seed["seed"], "theme": seed["theme"], "audience": seed["audience"],
            "ages": "-".join(map(str, seed["ages"])) if seed.get("ages") else "adult", "season": seed.get("season", ""),
            "median_price": s.get("median_price"), "median_reviews": reviews, "median_bsr": bsr,
            "median_pages": s.get("median_pages"), "common_trim": s.get("common_trim"), "easy_books": easy,
            "demand": round(demand, 3), "competition": round(competition, 3), "taste": round(taste_fit, 3),
            "difference": diff, "sales_fit": round(sales_fit, 3), "season_bonus": seed.get("season_bonus", 0),
            "score": round(total, 3), "status": "new"}


def save_rows(rows: list[dict]) -> None:
    path = settings.DATA / NICHES_CSV
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in FIELDS})


def books_getting_ready() -> int:
    active = ("planned", "drawing", "built", "listed", "drafted", "waiting_approval")
    return sum(1 for b in jobs.all_books() if b["stage"] in active and not b.get("on_hold"))


def reason_line(row: dict, data: dict) -> str:
    bits = []
    if row["season"]:
        bits.append(f"seasonal: {row['season']}")
    bits.append(f"demand {'good' if row['demand'] > 0.5 else 'ok' if row['demand'] > 0.25 else 'low'} (BSR ~{int(row['median_bsr']):,})")
    bits.append(f"competition {'light' if row['competition'] > 0.5 else 'medium' if row['competition'] > 0.3 else 'heavy'} "
                f"(~{int(row['median_reviews'])} reviews)")
    if row["easy_books"]:
        bits.append(f"{row['easy_books']} top-10 books with <50 reviews still selling")
    if row["taste"] > 0.6:
        bits.append("fits your taste")
    top2 = [r for r in data["top"][:2]]
    comp = "; ".join(f"${r.get('price')} / {r.get('reviews')} reviews" for r in top2)
    return ", ".join(bits) + (f". Top 2: {comp}" if comp else "")


async def research_round(force: bool = False) -> list[dict]:
    """Research seeds and send you the top 3. Returns the scored rows."""
    from .telegram_bot import ask_me, notify
    need = settings.load_config()["agent"]["books_per_week"]
    if not force and books_getting_ready() >= need:
        return []
    seen = settings.read_json(settings.DATA / "inbox_seen.json", [])
    if not force and any(p.name not in seen for p in settings.INBOX.glob("*.pdf")):
        return []   # finish the PDFs you gave me first
    last = settings.read_json(settings.DATA / "research" / "_last_round.json", {})
    if not force and last.get("date") == date.today().isoformat():
        return []
    budget = settings.load_config()["research"]["searches_per_day"]
    candidates = sorted(seeds(), key=lambda s: -(s.get("season_bonus", 0) * 2 + taste.word_score(s["seed"])
                                                  + difference(s)))
    rows, datas = [], {}
    for s in candidates[:budget]:
        try:
            data = await research.research(s["seed"], s.get("audience_words"))
        except research.DailyLimit:
            break
        row = score(s, data)
        row["_seed"] = s
        rows.append(row)
        datas[s["seed"]] = data
    settings.write_json(settings.DATA / "research" / "_last_round.json", {"date": date.today().isoformat()})
    if not rows:
        return []
    rows.sort(key=lambda r: -r["score"])
    save_rows(rows)
    top = rows[:3]
    level = state.load()["level"]
    if level >= 2:
        pick = top[0]
        await notify(f"🧭 Level 2: I picked the niche '{pick['seed']}'. Why: {reason_line(pick, datas[pick['seed']])}.\n"
                     "I'll still ask you before publishing. /level 1 to go back to choosing yourself.")
        taste.record("niche", "auto", pick["seed"], reason="level 2 auto-pick")
        return [start_book(pick, datas[pick["seed"]])]
    lines = ["🧭 Book ideas for next week (from Amazon research):"]
    for i, r in enumerate(top, 1):
        lines.append(f"\n{i}. {r['seed']} (score {r['score']:.2f})\n   {reason_line(r, datas[r['seed']])}")
    answer = await ask_me("\n".join(lines), buttons=[str(i) for i in range(1, len(top) + 1)] + ["None"],
                          typed_for=["None"], about="niche")
    if not answer:
        return rows
    if answer["choice"] and answer["choice"].isdigit():
        pick = top[int(answer["choice"]) - 1]
        for r in top:
            if r is not pick:
                taste.record("niche", "skipped", r["seed"])
        taste.record("niche", "approved", pick["seed"], reason=answer.get("text"), changed=False)
        await _offer_level2()
        return [start_book(pick, datas[pick["seed"]])]
    reason = answer.get("text") or ""
    for r in top:
        taste.record("niche", "rejected", r["seed"], reason=reason)
    await notify("👍 Noted. I'll use your reason to pick better ideas next time.")
    return rows


async def _offer_level2() -> None:
    from .telegram_bot import notify
    streak = taste.load().get("approval_streak", 0)
    need = settings.load_config()["autonomy"]["level2_after_approvals"]
    if state.load()["level"] == 1 and streak >= need:
        decisions.log({"type": "level2_offer", "streak": streak})
        await notify(
            f"⭐ You've approved {streak} of my niche ideas in a row unchanged. If you like, send /level 2 and I'll "
            "pick niches myself (I'll still ask before every publish). Only you can change the level.")


def start_book(row: dict, data: dict) -> dict:
    seed = row["_seed"]
    problem = trademark_problem(seed["seed"])
    if problem:
        raise ValueError(f"Niche rejected: {problem}")
    title = row["theme"]
    slug = jobs.make_slug(f"{row['theme']} {row['audience'].split()[0]}")
    if jobs.load_book(slug):
        slug = f"{slug}-{datetime.now(timezone.utc).strftime('%m%d')}"
    jobs.create_book(title, slug=slug)
    from .pipeline import save_plan
    save_plan(slug, {"source": "new", "theme": row["theme"], "audience": seed["audience"], "ages": seed.get("ages"),
                     "seed": seed["seed"], "audience_words": seed.get("audience_words", []),
                     "season": seed.get("season"), "research_summary": data["summary"],
                     "formats": ["paperback", "hardcover"]})
    decisions.log({"type": "niche_started", "slug": slug, "seed": seed["seed"]})
    return {"slug": slug, **row}


async def periodic() -> None:
    marker = settings.DATA / "research" / "_last_attempt.json"
    last = settings.read_json(marker, {}).get("at")
    if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=3):
        return    # tried recently - don't hammer Amazon
    settings.write_json(marker, {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    try:
        await research_round()
    except UsageLimit:
        raise
    except Exception:
        log.exception("Research round failed")

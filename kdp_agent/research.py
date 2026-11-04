"""Amazon research with the separate, logged-out research browser profile.

- Search suggestions: seed + a-z, seed + "for", seed + audience words.
- Top 10 books for the seed: title, price, reviews, BSR, pages, trim size.
Browses slowly (2-5 s pauses), stops at any CAPTCHA, never uses the KDP login.
Results are cached in data/research/ for a few days.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from . import settings
from .browser import goto, guard, human_pause, profile_session

log = logging.getLogger("research")

SUGGEST_URL = ("https://completion.amazon.com/api/2017/suggestions?limit=11&prefix={q}"
               "&suggestion-type=KEYWORD&page-type=Search&alias=stripbooks&site-variant=desktop"
               "&mid=ATVPDKIKX0DER&lop=en_US")

# Runs inside the browser on an Amazon search results page.
SEARCH_JS = r"""
() => [...document.querySelectorAll('div[data-component-type="s-search-result"]')].map(card => {
  const txt = sel => (card.querySelector(sel)?.textContent || '').trim();
  const sponsored = /Sponsored/i.test(card.innerText.slice(0, 300));
  const title = txt('h2 span') || txt('h2');
  const price = txt('.a-price .a-offscreen');
  const ratingLabel = card.querySelector('[aria-label*="out of 5 stars"]')?.getAttribute('aria-label') || '';
  const reviewsLabel = card.querySelector('a[href*="customerReviews"] span, span.s-underline-text')?.textContent || '';
  const link = card.querySelector('h2 a, a.a-link-normal.s-no-outline')?.getAttribute('href') || '';
  return {asin: card.getAttribute('data-asin'), title, price, rating: ratingLabel, reviews: reviewsLabel,
          url: link, sponsored, format: (card.innerText.match(/Paperback|Hardcover|Kindle/) || [''])[0]};
})
"""


def _num(text: str) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def clean_search_results(raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        if not r.get("asin") or r.get("sponsored") or not r.get("title"):
            continue
        reviews_text = re.sub(r"[()]", "", r.get("reviews", ""))
        reviews = _num(reviews_text.replace("K", "000").replace("k", "000")) if reviews_text else 0
        out.append({"asin": r["asin"], "title": r["title"], "price": _num(r.get("price", "")),
                    "rating": _num(r.get("rating", "")), "reviews": int(reviews or 0),
                    "format": r.get("format", "")})
    return out


def parse_product_text(text: str) -> dict:
    """Read BSR, page count and size from a product page's text."""
    info: dict = {}
    m = re.search(r"Best Sellers Rank:?\s*#\s*([\d,]+)\s+in\s+Books", text, re.I)
    if m:
        info["bsr"] = int(m.group(1).replace(",", ""))
    m = re.search(r"(?:Print length|Paperback|Hardcover)\s*[:‏‎‏ ]*\s*([\d,]+)\s*pages", text, re.I)
    if m:
        info["pages"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Dimensions\s*[:‏‎‏ ]*\s*([\d.]+)\s*x\s*([\d.]+)\s*x\s*([\d.]+)\s*inches", text, re.I)
    if m:
        dims = sorted(float(x) for x in m.groups())
        info["trim"] = f"{dims[1]:g}x{dims[2]:g}"   # thickness is the smallest number
    m = re.search(r"Reading age\s*[:‏‎‏ ]*\s*([^\n]+)", text, re.I)
    if m:
        info["reading_age"] = m.group(1).strip()[:40]
    return info


def _cache_path(seed: str):
    return settings.DATA / "research" / (re.sub(r"[^a-z0-9]+", "-", seed.lower()).strip("-") + ".json")


def cached(seed: str) -> dict | None:
    data = settings.read_json(_cache_path(seed), None)
    if not data:
        return None
    days = settings.load_config()["research"]["cache_days"]
    if datetime.fromisoformat(data["fetched_at"]) < datetime.now(timezone.utc) - timedelta(days=days):
        return None
    return data


def _count_today() -> int:
    log_file = settings.DATA / "research" / "_searches.json"
    data = settings.read_json(log_file, {})
    return data.get(datetime.now(timezone.utc).date().isoformat(), 0)


def _add_today() -> None:
    log_file = settings.DATA / "research" / "_searches.json"
    data = settings.read_json(log_file, {})
    key = datetime.now(timezone.utc).date().isoformat()
    data = {key: data.get(key, 0) + 1}
    settings.write_json(log_file, data)


class DailyLimit(Exception):
    pass


async def research(seed: str, audience_words: list[str] | None = None, letters: str = string.ascii_lowercase,
                   top_n: int = 10, force: bool = False) -> dict:
    """Full research for one seed phrase. Uses the cache when fresh."""
    if not force:
        hit = cached(seed)
        if hit:
            return hit
    if _count_today() >= settings.load_config()["research"]["searches_per_day"]:
        raise DailyLimit("Today's research budget is used up - I'll continue tomorrow.")
    _add_today()
    base = settings.load_config()["browser"]["amazon_base_url"]
    prefixes = [seed] + [f"{seed} {c}" for c in letters] + [f"{seed} for"] + \
               [f"{seed} for {w}" for w in (audience_words or [])]
    suggestions: list[str] = []
    async with profile_session("research") as ctx:
        page = await ctx.new_page()
        for prefix in prefixes:
            try:
                resp = await ctx.request.get(SUGGEST_URL.format(q=quote_plus(prefix)))
                if resp.ok:
                    for s in (await resp.json()).get("suggestions", []):
                        v = s.get("value", "").strip()
                        if v and v not in suggestions:
                            suggestions.append(v)
            except Exception as exc:
                log.warning("Suggestion lookup failed for %r: %s", prefix, exc)
            await human_pause(0.6)
        await goto(page, f"{base}/s?k={quote_plus(seed)}&i=stripbooks", f"searching Amazon for '{seed}'")
        results = clean_search_results(await page.evaluate(SEARCH_JS))[:top_n]
        for r in results:
            await goto(page, f"{base}/dp/{r['asin']}", f"reading a competitor book ({r['asin']})")
            try:
                text = await page.inner_text("#dp", timeout=10000)
            except Exception:
                text = await page.inner_text("body")
            r.update(parse_product_text(text))
            await guard(page, "reading competitor details")
    data = {"seed": seed, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "suggestions": suggestions, "top": results, "summary": summarise(results)}
    settings.write_json(_cache_path(seed), data)
    return data


def summarise(top: list[dict]) -> dict:
    def med(key):
        vals = [r[key] for r in top if r.get(key)]
        return statistics.median(vals) if vals else None
    trims = [r["trim"] for r in top if r.get("trim")]
    return {"median_price": med("price"), "median_reviews": med("reviews"), "median_bsr": med("bsr"),
            "median_pages": med("pages"),
            "common_trim": max(set(trims), key=trims.count) if trims else None,
            "prices": sorted(r["price"] for r in top if r.get("price")),
            "count": len(top)}


def research_notes(data: dict) -> str:
    """A short text version for Claude prompts and Telegram."""
    s = data["summary"]
    lines = [f"Seed: {data['seed']} (fetched {data['fetched_at'][:10]})",
             f"Median price ${s['median_price']}, median reviews {s['median_reviews']}, "
             f"median BSR {s['median_bsr']}, median pages {s['median_pages']}, common trim {s['common_trim']}",
             "Search suggestions: " + "; ".join(data["suggestions"][:40]),
             "Top books:"]
    for r in data["top"]:
        lines.append(f"- {r['title'][:90]} | ${r.get('price')} | {r.get('reviews')} reviews | "
                     f"BSR {r.get('bsr')} | {r.get('pages')} pages | {r.get('trim')}")
    return "\n".join(lines)


def save_json(path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

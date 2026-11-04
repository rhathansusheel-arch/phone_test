"""Listing writer: title, subtitle, description, 7 keywords, 3 categories, price options.

Follows .claude/skills/kdp-listing-seo/SKILL.md. Claude writes the words; this file checks
every rule and sends Claude back to fix anything that breaks one.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics

from . import settings, specs, taste
from .claude_cli import ask_claude, extract_json
from .errors import NeedsHuman
from .safety import banned_listing_words, find_trademarks

log = logging.getLogger("listing")

SKILL_FILE = settings.ROOT / ".claude" / "skills" / "kdp-listing-seo" / "SKILL.md"
STOP_WORDS = {"a", "an", "and", "the", "for", "of", "to", "with", "in", "on", "by", "at", "or", "&", "-",
              "ages", "age", "kids", "book", "books"}
MEDICAL = re.compile(r"\b(cures?|heal(s|ing)?|therap(y|eutic)|treat(s|ment)?|clinically|diagnos\w*|"
                     r"relieves? anxiety|reduces? anxiety|adhd|prevents?)\b", re.I)
COUNT_CLAIM = re.compile(r"\b(\d{1,3})\s+(?:unique\s+|different\s+|big\s+|bold\s+|simple\s+|easy\s+|fun\s+|cute\s+)*"
                         r"(pictures|designs|illustrations|colou?ring pages|images|drawings)\b", re.I)

# Subject words we can check against the pictures actually in the book.
KNOWN_SUBJECT_WORDS = set("""shark whale dolphin octopus turtle jellyfish seahorse crab starfish fish seal penguin
lobster squid stingray narwhal otter walrus pufferfish clownfish mermaid submarine
car truck bus tractor train plane airplane helicopter boat ship rocket digger excavator bulldozer
crane firetruck ambulance motorcycle bike bicycle scooter van taxi tank dumper cement
santa reindeer snowman elf gingerbread tree ornament stocking candle bell wreath present gift sleigh
dinosaur unicorn dragon cat dog horse cow pig sheep lion tiger elephant giraffe monkey bear owl""".split())

PROMPT = """You are writing an Amazon KDP listing for a colouring book. Follow these rules exactly:

{skill}

BOOK FACTS (everything you write must be true of these):
{facts}

AMAZON RESEARCH (real data from search suggestions and the top 10 books):
{research}

WHAT THE OWNER HAS TOLD ME BEFORE (their taste - respect it):
{taste}
{feedback}
Return ONLY a JSON object, no other text:
{{
  "title": "...",                     // also printed on the cover and title page
  "subtitle": "...",                  // also printed on the cover
  "description": "...",              // plain text, use \\n for new lines, bullets start with "• "
  "keywords": ["...", "...", "...", "...", "...", "...", "..."],
  "categories": ["Top > Sub > Leaf", "...", "..."],
  "reading_age": {{"min": 2, "max": 5}},  // or null for adults
  "badge": "Ages 2-5",               // short badge for the cover
  "cover_palette": ["#0b6fa4", "#3fc1e0", "#ffd23f"],  // cover colours: dark main, light second, bright accent
  "blurb": "...",                    // back cover text, max 450 characters, \\n allowed, bullets "• "
  "main_phrase": "...",
  "research_notes": "..."            // 2-4 lines: which suggestions/competitors drove your choices
}}"""


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        if w in STOP_WORDS or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def validate(listing: dict, facts: dict) -> tuple[list[str], list[str]]:
    """Returns (problems that must be fixed, warnings)."""
    problems, warnings = [], []
    title, subtitle = listing.get("title", "").strip(), listing.get("subtitle", "").strip()
    pen = settings.load_config()["agent"]["pen_name"]
    if not title:
        problems.append("Title is empty.")
    if len(title) + len(subtitle) + 2 >= 200:
        problems.append(f"Title + subtitle is {len(title) + len(subtitle) + 2} characters; must be under 200.")
    for field in ("title", "subtitle"):
        bad = banned_listing_words(listing.get(field, ""))
        if bad:
            problems.append(f"{field} contains banned words: {', '.join(bad)}.")
    if re.search(r"<[^>]+>", title + subtitle):
        problems.append("No HTML in title or subtitle.")
    kws = listing.get("keywords", [])
    if len(kws) != 7:
        problems.append(f"Need exactly 7 keyword boxes, got {len(kws)}.")
    taken = _words(f"{title} {subtitle} {pen}")
    cat_words = _words(" ".join(listing.get("categories", [])))
    seen = set()
    for i, kw in enumerate(kws, 1):
        if len(kw) > 50:
            problems.append(f"Keyword {i} is {len(kw)} characters (max 50): '{kw}'.")
        if re.search(r"[,\"“”]", kw):
            problems.append(f"Keyword {i} has a comma or quote: '{kw}'.")
        bad = banned_listing_words(kw)
        if bad:
            problems.append(f"Keyword {i} has banned words ({', '.join(bad)}): '{kw}'.")
        repeats = _words(kw) & taken
        if repeats:
            problems.append(f"Keyword {i} repeats words already in the title/subtitle/author "
                            f"({', '.join(sorted(repeats))}): '{kw}'.")
        cat_rep = _words(kw) & cat_words - taken
        if cat_rep:
            warnings.append(f"Keyword {i} repeats category words ({', '.join(sorted(cat_rep))}).")
        if kw.lower() in seen:
            problems.append(f"Keyword {i} is a duplicate.")
        seen.add(kw.lower())
    if len(listing.get("categories", [])) != 3:
        problems.append(f"Need exactly 3 categories, got {len(listing.get('categories', []))}.")
    desc = listing.get("description", "")
    if not re.search(r"inside you(?:'|’)ll find", desc, re.I):
        problems.append("Description must contain \"Inside you'll find:\".")
    bullets = [l for l in desc.splitlines() if l.strip().startswith(("•", "-", "*"))]
    if not 5 <= len(bullets) <= 7:
        problems.append(f"Description needs 5-7 bullets, has {len(bullets)}.")
    if re.search(r"best\s*-?\s*sell|#1|number one", desc, re.I):
        problems.append("Description must not claim bestseller status.")
    med = MEDICAL.findall(desc + " " + listing.get("blurb", ""))
    if med:
        problems.append("No medical claims (found: " + ", ".join(sorted({m[0] for m in med})) + ").")
    for n, what in COUNT_CLAIM.findall(desc + " " + listing.get("blurb", "") + " " + subtitle):
        if int(n) != facts["picture_count"]:
            problems.append(f"Says {n} {what} but the book has {facts['picture_count']} pictures.")
    if len(listing.get("blurb", "")) > 450:
        problems.append("Back-cover blurb is over 450 characters.")
    tms = find_trademarks(title, subtitle, desc, listing.get("blurb", ""), " ".join(kws),
                          " ".join(listing.get("categories", [])))
    if tms:
        problems.append("Trademarks/brands/characters are not allowed: " + ", ".join(tms) + ".")
    facts_text = json.dumps(facts).lower()
    for claim in ("large print", "tracing", "dot to dot", "sticker", "maze", "puzzle", "word search",
                  "glitter", "perforated", "single sided", "single-sided", "certificate"):
        if claim in f"{title} {subtitle} {desc} {' '.join(kws)}".lower() and claim not in facts_text:
            problems.append(f"'{claim}' is not true of this book - remove it.")
    subjects = [s.lower() for s in facts.get("subjects", [])]
    if subjects:
        for n, kw in enumerate(kws, 1):
            for animal in re.findall(r"[a-z]+", kw.lower()):
                if animal in KNOWN_SUBJECT_WORDS and not any(animal.rstrip("s") in s for s in subjects):
                    problems.append(f"Keyword {n} mentions '{animal}' but there is no {animal} picture in the book.")
    everything = f"{title} {subtitle} {desc} {' '.join(kws)}".lower()
    if "colouring" not in everything or "coloring" not in everything:
        warnings.append("Tip: use both 'coloring' and 'colouring' somewhere.")
    return problems, warnings


async def write_listing(facts: dict, research_text: str, feedback: str | None = None,
                        previous: dict | None = None, attempts: int = 3) -> dict:
    skill = SKILL_FILE.read_text(encoding="utf-8") if SKILL_FILE.exists() else ""
    taste_text = "\n".join(taste.reasons(last=15)) or "(nothing yet)"
    extra = ""
    if previous:
        extra += f"\nTHE PREVIOUS VERSION (improve it, keep what's good):\n{json.dumps(previous, indent=1)}\n"
    if feedback:
        extra += f"\nTHE OWNER ASKED FOR THIS CHANGE (do it):\n{feedback}\n"
    prompt = PROMPT.format(skill=skill, facts=json.dumps(facts, indent=1), research=research_text or "(none)",
                           taste=taste_text, feedback=extra)
    problems: list[str] = []
    listing: dict = {}
    for attempt in range(1, attempts + 1):
        if problems:
            prompt_now = prompt + "\nYOUR LAST ANSWER BROKE THESE RULES - fix every one:\n- " + \
                "\n- ".join(problems) + f"\nLast answer:\n{json.dumps(listing, indent=1)}"
        else:
            prompt_now = prompt
        listing = extract_json(await ask_claude(prompt_now))
        listing["keywords"] = [k.strip() for k in listing.get("keywords", [])]
        problems, warnings = validate(listing, facts)
        log.info("Listing attempt %d: %d problems", attempt, len(problems))
        if not problems:
            listing["warnings"] = warnings
            return listing
    if any("Trademark" in p for p in problems):
        raise NeedsHuman("The listing kept including a trademark: " + "; ".join(problems))
    listing["warnings"] = ["NOT FIXED: " + p for p in problems]
    return listing


# ------------------------------------------------------------ prices

def _snap(price: float) -> float:
    """Round to the nearest .99 (e.g. 8.6 -> 8.99, 9.2 -> 8.99)."""
    return max(0.99, math.floor(price + 0.5) - 0.01)


def _option(fmt: str, trim: str, pages: int, price: float) -> dict:
    cost = specs.printing_cost(fmt, trim, pages)
    royalty = specs.royalty(price, cost)
    inr = settings.load_config()["money"]["usd_to_inr"]
    return {"price": round(price, 2), "printing_cost": cost, "rate": specs.royalty_rate(price),
            "royalty": royalty, "royalty_inr": round(royalty * inr)}


def price_options(fmt: str, trim: str, pages: int, competitor_prices: list[float] | None,
                  paperback_price: float | None = None) -> dict:
    """Two price options that match competitors and stay above KDP's minimum."""
    cost = specs.printing_cost(fmt, trim, pages)
    floor = _snap(specs.min_price(cost) + 0.5)
    if fmt == "hardcover":
        pb_cost = specs.printing_cost("paperback", trim, pages)
        base = (paperback_price or 8.99) + (cost - pb_cost) / 0.6 + 2
        candidates = [_snap(base), _snap(base) + 2]
        median = None
    else:
        prices = [p for p in (competitor_prices or []) if p and 3 < p < 60]
        median = statistics.median(prices) if prices else 8.99
        a = _snap(median)
        b = 9.99 if a < 9.99 and median * 1.25 >= 9.99 else (a - 1 if a > 9.99 else a + 1)
        candidates = [a, b]
    options = []
    for c in candidates:
        c = max(c, floor)
        if c not in [o["price"] for o in options]:
            options.append(_option(fmt, trim, pages, c))
    if len(options) < 2:
        options.append(_option(fmt, trim, pages, options[-1]["price"] + 1))
    # Recommend the higher royalty unless it's far above what competitors charge
    rec = max(range(len(options)), key=lambda i: options[i]["royalty"])
    if median and options[rec]["price"] > median * 1.2:
        rec = min(range(len(options)), key=lambda i: abs(options[i]["price"] - median))
    return {"options": options, "recommended": rec, "competitor_median": median, "min_price": floor,
            "other_marketplaces": "KDP fills these in automatically from the Amazon.com price"}


def listing_text(listing: dict, prices: dict | None = None) -> str:
    """The listing as a Telegram message."""
    lines = [f"📕 {listing['title']}", f"{listing.get('subtitle', '')}", "",
             listing.get("description", ""), "", "🔑 Keywords:"]
    lines += [f"  {i}. {k}" for i, k in enumerate(listing.get("keywords", []), 1)]
    lines += ["", "📂 Categories:"] + [f"  • {c}" for c in listing.get("categories", [])]
    if prices:
        for fmt, p in prices.items():
            lines.append(f"\n💲 {fmt.title()} price options:")
            for i, o in enumerate(p["options"]):
                star = " ⭐ recommended" if i == p["recommended"] else ""
                lines.append(f"  {chr(65 + i)}) ${o['price']:.2f} → royalty ${o['royalty']:.2f} "
                             f"(≈ ₹{o['royalty_inr']}) per sale, printing ${o['printing_cost']:.2f}{star}")
    if listing.get("warnings"):
        lines += ["", "⚠️ " + "\n⚠️ ".join(listing["warnings"])]
    return "\n".join(lines)

"""KDP's printing numbers: trim sizes, margins, spine, cover size, printing cost, royalty.

Numbers come from config.yaml (kdp_specs), overridden by data/kdp_specs.json once
/checkspecs has confirmed them from KDP's own help pages.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import date

from . import settings

log = logging.getLogger("specs")

SPECS_FILE = "kdp_specs.json"


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get() -> dict:
    base = settings.load_config()["kdp_specs"]
    override = settings.read_json(settings.DATA / SPECS_FILE, {})
    return _merge(base, override)


def verified() -> bool:
    return bool(get().get("verified_from_kdp"))


def parse_trim(trim: str) -> tuple[float, float]:
    w, h = trim.lower().replace(" ", "").replace("in", "").split("x")
    return float(w), float(h)


def trim_key(w: float, h: float) -> str:
    def fmt(x: float) -> str:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{fmt(w)}x{fmt(h)}"


def paperback_trims() -> dict[str, int]:
    return {k: int(v) for k, v in get()["paperback"]["trims"].items()}


def hardcover_trims() -> list[str]:
    return list(get()["hardcover"]["trims"])


def nearest_trim(w: float, h: float, allowed: list[str]) -> str:
    """The allowed trim closest in shape and size (used to resize a PDF)."""
    def distance(key: str) -> float:
        tw, th = parse_trim(key)
        return abs(tw - w) + abs(th - h) + 3 * abs(tw / th - w / h)
    return min(allowed, key=distance)


def gutter(pages: int) -> float:
    for max_pages, margin in get()["paperback"]["gutter"]:
        if pages <= max_pages:
            return float(margin)
    raise ValueError(f"{pages} pages is more than KDP allows")


def outside_margin() -> float:
    return float(get()["paperback"]["outside_margin_no_bleed"])


def is_large(trim: str) -> bool:
    w, h = parse_trim(trim)
    return w > 6.12 or h > 9.0


def printing_cost(fmt: str, trim: str, pages: int) -> float:
    """Printing cost in USD on Amazon.com (black ink, white paper)."""
    table = get()["printing_cost_usd"][fmt]["large" if is_large(trim) else "regular"]
    if pages <= table["short_max_pages"]:
        return round(table["short_flat"], 2)
    return round(table["fixed"] + table["per_page"] * pages, 2)


def royalty_rate(price: float) -> float:
    r = get()["royalty"]
    return r["high_rate"] if price >= r["threshold_usd"] - 1e-9 else r["low_rate"]


def royalty(price: float, cost: float) -> float:
    return round(royalty_rate(price) * price - cost, 2)


def min_price(cost: float) -> float:
    """Lowest list price KDP accepts: the one where royalty is not negative."""
    low = round(cost / get()["royalty"]["low_rate"] + 0.005, 2)
    return low


def spine_width(fmt: str, pages: int) -> float:
    s = get()[fmt]
    return round(pages * float(s["spine_per_page_white"]) + float(s.get("spine_extra", 0)), 4)


def paperback_cover_size(trim: str, pages: int) -> dict:
    s = get()["paperback"]
    w, h = parse_trim(trim)
    spine = spine_width("paperback", pages)
    bleed = float(s["cover_bleed"])
    return {"width": round(bleed + w + spine + w + bleed, 4), "height": round(bleed + h + bleed, 4),
            "spine": spine, "edge": bleed, "trim_w": w, "trim_h": h,
            "spine_text": pages >= int(s["spine_text_min_pages"]), "source": "formula"}


def hardcover_cover_size(trim: str, pages: int) -> dict:
    s = get()["hardcover"]
    w, h = parse_trim(trim)
    spine = spine_width("hardcover", pages)
    wrap = float(s["cover_wrap"])
    return {"width": round(wrap + w + spine + w + wrap, 4), "height": round(wrap + h + wrap, 4),
            "spine": spine, "edge": wrap, "hinge": float(s["cover_hinge"]), "trim_w": w, "trim_h": h,
            "spine_text": pages >= int(get()["paperback"]["spine_text_min_pages"]),
            "source": "formula (not confirmed)"}


def page_count_ok(fmt: str, trim: str, pages: int) -> str | None:
    """Returns a problem in plain words, or None if the page count is allowed."""
    if fmt == "paperback":
        trims = paperback_trims()
        if trim not in trims:
            return f"{trim} is not a KDP paperback size"
        lo, hi = int(get()["paperback"]["min_pages"]), trims[trim]
    else:
        if trim not in hardcover_trims():
            return f"{trim} is not a KDP hardcover size"
        lo, hi = int(get()["hardcover"]["min_pages"]), int(get()["hardcover"]["max_pages"])
    if pages < lo:
        return f"{pages} pages is too short ({fmt} needs at least {lo})"
    if pages > hi:
        return f"{pages} pages is too long ({fmt} allows at most {hi} at {trim})"
    return None


# ------------------------------------------------------------ /checkspecs

CHECK_PROMPT = """Read Amazon KDP's CURRENT official help pages on kdp.amazon.com (use WebFetch/WebSearch;
only trust kdp.amazon.com pages): Paperback Submission Guidelines, trim size/bleed/margins,
paperback cover guidelines, Hardcover guidelines (trim sizes, page counts, cover wrap/hinge/spine),
Printing Cost, and Paperback/Hardcover royalty. Today is {today}.

Return ONLY a JSON object with exactly this structure, filling in the numbers you found
(keep the existing number if a page does not say; put the page URLs you used in "sources",
and anything uncertain in "notes"):

{current}
"""


async def check_from_kdp() -> tuple[dict, list[str]]:
    """Ask Claude (on your PC) to read KDP's help pages. Returns (new specs, list of differences)."""
    from .claude_cli import ask_claude, extract_json
    current = get()
    current.pop("verified_from_kdp", None)
    template = dict(current, sources=[], notes="")
    answer = await ask_claude(
        CHECK_PROMPT.format(today=date.today().isoformat(), current=json.dumps(template, indent=2)),
        tools=["WebFetch", "WebSearch"], timeout=1200)
    found = extract_json(answer)
    diffs: list[str] = []

    def walk(a, b, path=""):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k in ("sources", "notes", "checked_on"):
                    continue
                walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        elif a != b:
            diffs.append(f"{path}: {a} → {b}")
    walk(current, found)
    return found, diffs


def save_checked(found: dict) -> None:
    found = dict(found, verified_from_kdp=True, checked_on=date.today().isoformat())
    settings.write_json(settings.DATA / SPECS_FILE, found)

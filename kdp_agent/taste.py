"""data/taste.json - every approval, rejection and your reasons. The agent learns from this."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import settings

FILE = "taste.json"


def load() -> dict:
    return settings.read_json(settings.DATA / FILE, {
        "events": [], "liked_words": {}, "disliked_words": {}, "approval_streak": 0})


def save(data: dict) -> None:
    settings.write_json(settings.DATA / FILE, data)


def record(kind: str, decision: str, about: str, reason: str | None = None, **details) -> dict:
    """kind: niche / book / pages / listing / publish / fix ... decision: approved / rejected / changed."""
    data = load()
    data["events"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "kind": kind, "decision": decision, "about": about,
                           "reason": reason, **details})
    import re
    stop = {"coloring", "colouring", "book", "for", "and", "the", "kids", "ages", "with", "adults"}
    words = [w for w in re.findall(r"[a-z]+", about.lower()) if len(w) > 2 and w not in stop]
    bucket = None
    if kind in ("niche", "book"):   # only real taste decisions teach me what you like
        bucket = "liked_words" if decision == "approved" else "disliked_words" if decision == "rejected" else None
    if bucket:
        for w in words:
            data[bucket][w] = data[bucket].get(w, 0) + 1
    if kind == "niche":
        if decision == "approved" and not details.get("changed"):
            data["approval_streak"] = data.get("approval_streak", 0) + 1
        elif decision in ("rejected", "changed") or details.get("changed"):
            data["approval_streak"] = 0      # "skipped" (the ideas you didn't pick) doesn't break the streak
    save(data)
    return data


def word_score(text: str) -> float:
    """How well a niche matches your taste: + for liked words, - for disliked ones."""
    data = load()
    import re
    score = 0.0
    for w in re.findall(r"[a-z]+", text.lower()):
        score += data["liked_words"].get(w, 0) * 1.0
        score -= data["disliked_words"].get(w, 0) * 1.5
    return score


def reasons(kind: str | None = None, last: int = 20) -> list[str]:
    ev = [e for e in load()["events"] if e.get("reason") and (kind is None or e["kind"] == kind)]
    return [f"{e['decision']} '{e['about']}': {e['reason']}" for e in ev[-last:]]


def summary() -> str:
    data = load()
    ev = data["events"]
    if not ev:
        return "Nothing learned yet. I'll learn from every approval, rejection and reason you give."
    counts = Counter((e["kind"], e["decision"]) for e in ev)
    liked = sorted(data["liked_words"].items(), key=lambda x: -x[1])[:8]
    disliked = sorted(data["disliked_words"].items(), key=lambda x: -x[1])[:8]
    lines = [f"Decisions so far: {len(ev)}",
             "  " + ", ".join(f"{k} {d}: {n}" for (k, d), n in counts.most_common(6)),
             "You like: " + (", ".join(w for w, _ in liked) or "-"),
             "You avoid: " + (", ".join(w for w, _ in disliked) or "-"),
             f"Niche approvals in a row: {data.get('approval_streak', 0)}"]
    recent = reasons(last=5)
    if recent:
        lines.append("Recent reasons:\n  " + "\n  ".join(recent))
    return "\n".join(lines)

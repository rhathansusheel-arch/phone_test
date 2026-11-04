"""KDP weekly limit guard (Hard rule 1).

Max N titles CREATED (saving a draft counts) and N SUBMITTED (first Publish) per format
per week. The week resets every Sunday 00:00 UTC (Sunday 5:30 AM IST).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import settings

FORMATS = ("paperback", "hardcover")
KINDS = ("created", "submitted")
IST = timezone(timedelta(hours=5, minutes=30))


class NoSlotLeft(Exception):
    """Raised when using a slot would break KDP's weekly limit."""


def week_start(now: datetime | None = None) -> datetime:
    """The most recent Sunday 00:00 UTC (the start of the current KDP week)."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    days_since_sunday = (now.weekday() + 1) % 7  # Monday=0 ... Sunday=6
    start = now - timedelta(days=days_since_sunday)
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def next_reset(now: datetime | None = None) -> datetime:
    return week_start(now) + timedelta(days=7)


def _limit(kind: str) -> int:
    cfg = settings.load_config()["kdp_limits"]
    return int(cfg[f"{kind}_per_week"])


def load(now: datetime | None = None) -> dict:
    """Read limits.json, starting a fresh count if a new week has begun."""
    current = week_start(now).isoformat()
    data = settings.read_json(settings.LIMITS_FILE, None)
    if not data or data.get("week_start") != current:
        data = {"week_start": current,
                **{fmt: {k: [] for k in KINDS} for fmt in FORMATS}}
        settings.write_json(settings.LIMITS_FILE, data)
    return data


def slots_left(fmt: str, kind: str, now: datetime | None = None) -> int:
    data = load(now)
    return max(0, _limit(kind) - len(data[fmt][kind]))


def use_slot(fmt: str, kind: str, slug: str, now: datetime | None = None) -> None:
    """Record a slot as used BEFORE doing the action on KDP. Refuses if none are left."""
    if fmt not in FORMATS or kind not in KINDS:
        raise ValueError(f"Unknown format/kind: {fmt}/{kind}")
    data = load(now)
    if slug in data[fmt][kind]:
        return  # already counted this week (e.g. retry after a crash)
    if len(data[fmt][kind]) >= _limit(kind):
        raise NoSlotLeft(f"No {fmt} '{kind}' slot left this week. Next reset: {reset_text(now)}")
    data[fmt][kind].append(slug)
    settings.write_json(settings.LIMITS_FILE, data)


def reset_text(now: datetime | None = None) -> str:
    return next_reset(now).astimezone(IST).strftime("%a %d %b, %I:%M %p IST")


def summary(now: datetime | None = None) -> str:
    lines = []
    for fmt in FORMATS:
        c = slots_left(fmt, "created", now)
        s = slots_left(fmt, "submitted", now)
        lines.append(f"{fmt.title()}: {c} create / {s} submit slots left")
    lines.append(f"Next reset: {reset_text(now)}")
    return "\n".join(lines)

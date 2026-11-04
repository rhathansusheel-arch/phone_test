"""Special 'stop' signals used across the agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


class UsageLimit(Exception):
    """Claude Pro usage limit reached. Wait until reset_at, then continue."""

    def __init__(self, message: str, reset_at: datetime | None = None):
        super().__init__(message)
        self.reset_at = reset_at

    def wait_seconds(self) -> float:
        if self.reset_at is None:
            return 60 * 60  # unknown: try again in an hour
        return max(60.0, (self.reset_at - datetime.now(timezone.utc)).total_seconds() + 120)

    def reset_text(self) -> str:
        if self.reset_at is None:
            return "about an hour"
        return self.reset_at.astimezone(IST).strftime("%I:%M %p IST, %a %d %b")


class NeedsHuman(Exception):
    """Something only you can do (login, code, CAPTCHA, KDP error). The book goes on hold."""


class StopBook(Exception):
    """Stop work on this book (e.g. you tapped Cancel). The book goes on hold quietly."""


class FieldNotFound(NeedsHuman):
    """A field on KDP could not be found by its label - KDP may have changed. Never guess."""

"""Job runner: moves each book through its stages and remembers where it stopped.

Every book has books/<slug>/status.json with its current stage. The stage is saved
after each step finishes, so after a crash or restart the agent carries on from the
last saved stage. (A step can run twice after a crash, so every step must be safe
to repeat.)

Stages, in order:
    planned -> drawing -> built -> listed -> drafted -> waiting_approval -> in_review -> live

Workers for each stage are added with @stage_worker("stage_name") (see pipeline.py).
Each book runs as its own task, so one book waiting for your tap never blocks another.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from . import settings, state
from .errors import NeedsHuman, StopBook, UsageLimit

log = logging.getLogger("jobs")

STAGES = ["planned", "drawing", "built", "listed", "drafted",
          "waiting_approval", "in_review", "live"]

# A worker gets the book's status dict and returns the next stage name,
# or None to stay in the same stage (e.g. still waiting for KDP review).
Worker = Callable[[dict], Awaitable[str | None]]
WORKERS: dict[str, Worker] = {}

# Set by the Telegram bot so the runner can message you.
notify: Callable[[str], Awaitable[None]] | None = None

_running: dict[str, asyncio.Task] = {}


def stage_worker(stage: str):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")

    def register(fn: Worker) -> Worker:
        WORKERS[stage] = fn
        return fn
    return register


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return now_utc().isoformat(timespec="seconds")


def make_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def book_dir(slug: str):
    return settings.BOOKS / slug


def status_path(slug: str):
    return book_dir(slug) / "status.json"


def create_book(title: str, slug: str | None = None, stage: str = "planned", **extra) -> dict:
    slug = slug or make_slug(title)
    if status_path(slug).exists():
        raise FileExistsError(f"A book called '{slug}' already exists.")
    status = {"slug": slug, "title": title, "stage": stage, "error": None,
              "on_hold": False, "wait_until": None,
              "history": [{"stage": stage, "at": _now()}], "updated_at": _now(), **extra}
    settings.write_json(status_path(slug), status)
    return status


def load_book(slug: str) -> dict:
    return settings.read_json(status_path(slug), None)


def update_book(slug: str, **fields) -> dict:
    status = load_book(slug)
    status.update(fields)
    status["updated_at"] = _now()
    settings.write_json(status_path(slug), status)
    return status


def set_stage(slug: str, stage: str, note: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    status = load_book(slug)
    status["stage"] = stage
    status["error"] = None
    status["wait_until"] = None
    entry = {"stage": stage, "at": _now()}
    if note:
        entry["note"] = note
    status["history"].append(entry)
    status["updated_at"] = _now()
    settings.write_json(status_path(slug), status)
    return status


def set_error(slug: str, message: str) -> None:
    update_book(slug, error=message)


def snooze(slug: str, seconds: float) -> None:
    """Don't work on this book again until `seconds` from now."""
    update_book(slug, wait_until=(now_utc() + timedelta(seconds=seconds)).isoformat(timespec="seconds"))


def all_books() -> list[dict]:
    books = []
    for path in sorted(settings.BOOKS.glob("*/status.json")):
        status = settings.read_json(path, None)
        if status:
            books.append(status)
    return books


def _is_due(book: dict) -> bool:
    if book.get("on_hold") or book["stage"] == "live" or book["stage"] not in WORKERS:
        return False
    wait = book.get("wait_until")
    return not wait or datetime.fromisoformat(wait) <= now_utc()


async def _tell(text: str) -> None:
    if notify:
        try:
            await notify(text)
        except Exception:
            log.exception("Could not send a Telegram message")


async def run_step(book: dict) -> bool:
    """Run one step for one book. Returns True if a step ran (even if it failed)."""
    slug, stage = book["slug"], book["stage"]
    worker = WORKERS[stage]
    state.set_doing(f"{book['title']}: {stage}")
    log.info("Working on %s (stage %s)", slug, stage)
    try:
        next_stage = await worker(book)
    except UsageLimit as exc:
        snooze(slug, exc.wait_seconds())
        state.update(usage_limit_until=exc.reset_at.isoformat() if exc.reset_at else None)
        log.warning("Claude usage limit hit; %s waits until %s", slug, exc.reset_text())
        await _tell(f"⏳ Claude usage limit reached. I saved my place and will continue "
                    f"'{book['title']}' after {exc.reset_text()}.")
        return True
    except NeedsHuman as exc:
        update_book(slug, on_hold=True, error=str(exc))
        await _tell(f"✋ '{book['title']}' is on hold: {exc}\nSend /unhold {slug} when it's sorted.")
        return True
    except StopBook as exc:
        update_book(slug, on_hold=True, error=str(exc))
        log.warning("Stopped %s: %s", slug, exc)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # keep going with other books; tell the user
        log.exception("Step failed for %s", slug)
        set_error(slug, f"{type(exc).__name__}: {exc}")
        snooze(slug, 30 * 60)  # try again in 30 minutes
        await _tell(f"⚠️ Problem with '{book['title']}' at stage '{stage}':\n{exc}\n"
                    f"I'll try again in 30 minutes. /hold {slug} stops it.")
        return True
    finally:
        state.set_doing("idle")
    if next_stage and next_stage != stage:
        set_stage(slug, next_stage)
        log.info("%s moved %s -> %s", slug, stage, next_stage)
    return True


async def run_once() -> int:
    """Run one step for every book that has work due, side by side. Returns how many ran."""
    if state.is_paused():
        return 0
    due = [b for b in all_books() if _is_due(b) and b["slug"] not in _running]
    results = await asyncio.gather(*(run_step(b) for b in due))
    return sum(results)


# Extra jobs that run on a timer (research, sales, backups...). Added by other modules.
PERIODIC: list[Callable[[], Awaitable[None]]] = []


_usage_wait_until: datetime | None = None


async def _run_timer(job) -> None:
    global _usage_wait_until
    if _usage_wait_until and now_utc() < _usage_wait_until:
        return
    try:
        await job()
    except UsageLimit as exc:
        _usage_wait_until = now_utc() + timedelta(seconds=exc.wait_seconds())
        log.warning("Usage limit in a timer job; waiting until %s", exc.reset_text())
    except Exception:
        log.exception("Timer job failed: %s", getattr(job, "__name__", job))


async def run_forever(every_seconds: int) -> None:
    log.info("Job runner started (checks every %ss)", every_seconds)
    while True:
        try:
            if not state.is_paused():
                for book in all_books():
                    slug = book["slug"]
                    if slug in _running or not _is_due(book):
                        continue
                    task = asyncio.create_task(run_step(book))
                    _running[slug] = task
                    task.add_done_callback(lambda _t, s=slug: _running.pop(s, None))
                for job in PERIODIC:
                    name = "timer:" + getattr(job, "__module__", "") + "." + getattr(job, "__name__", str(job))
                    if name in _running:
                        continue   # still busy (maybe waiting for your answer)
                    task = asyncio.create_task(_run_timer(job))
                    _running[name] = task
                    task.add_done_callback(lambda _t, n=name: _running.pop(n, None))
        except Exception:
            log.exception("Job runner error")
        await asyncio.sleep(every_seconds)

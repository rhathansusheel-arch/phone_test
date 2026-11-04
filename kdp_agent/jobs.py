"""Job runner: moves each book through its stages and remembers where it stopped.

Every book has books/<slug>/status.json with its current stage. The stage is saved
after each step finishes, so after a crash or restart the agent carries on from the
last saved stage. (A step can run twice after a crash, so every step must be safe
to repeat.)

Stages, in order:
    planned -> drawing -> built -> listed -> drafted -> waiting_approval -> in_review -> live

Later phases add a "worker" for each stage with @stage_worker("stage_name").
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import settings, state

log = logging.getLogger("jobs")

STAGES = ["planned", "drawing", "built", "listed", "drafted",
          "waiting_approval", "in_review", "live"]

# A worker gets the book's status dict and returns the next stage name,
# or None to stay in the same stage (e.g. still waiting for KDP review).
Worker = Callable[[dict], Awaitable[str | None]]
WORKERS: dict[str, Worker] = {}

# Set by the Telegram bot so the runner can message you about errors.
notify: Callable[[str], Awaitable[None]] | None = None


def stage_worker(stage: str):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")

    def register(fn: Worker) -> Worker:
        WORKERS[stage] = fn
        return fn
    return register


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def status_path(slug: str):
    return settings.BOOKS / slug / "status.json"


def create_book(title: str, slug: str | None = None, stage: str = "planned") -> dict:
    slug = slug or make_slug(title)
    if status_path(slug).exists():
        raise FileExistsError(f"A book called '{slug}' already exists.")
    status = {"slug": slug, "title": title, "stage": stage, "error": None,
              "history": [{"stage": stage, "at": _now()}], "updated_at": _now()}
    settings.write_json(status_path(slug), status)
    return status


def load_book(slug: str) -> dict:
    return settings.read_json(status_path(slug), None)


def set_stage(slug: str, stage: str, note: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    status = load_book(slug)
    status["stage"] = stage
    status["error"] = None
    entry = {"stage": stage, "at": _now()}
    if note:
        entry["note"] = note
    status["history"].append(entry)
    status["updated_at"] = _now()
    settings.write_json(status_path(slug), status)
    return status


def set_error(slug: str, message: str) -> None:
    status = load_book(slug)
    status["error"] = message
    status["updated_at"] = _now()
    settings.write_json(status_path(slug), status)


def all_books() -> list[dict]:
    books = []
    for path in sorted(settings.BOOKS.glob("*/status.json")):
        status = settings.read_json(path, None)
        if status:
            books.append(status)
    return books


async def run_once() -> int:
    """Do one step for every book that has work to do. Returns how many steps ran."""
    if state.is_paused():
        return 0
    steps = 0
    for book in all_books():
        if state.is_paused():  # you may tap /pause in the middle
            break
        stage = book["stage"]
        worker = WORKERS.get(stage)
        if stage == "live" or worker is None:
            continue
        slug = book["slug"]
        state.set_doing(f"{book['title']}: {stage}")
        log.info("Working on %s (stage %s)", slug, stage)
        try:
            next_stage = await worker(book)
        except Exception as exc:  # keep going with other books; tell the user
            log.exception("Step failed for %s", slug)
            set_error(slug, f"{type(exc).__name__}: {exc}")
            if notify:
                await notify(f"⚠️ Problem with '{book['title']}' at stage '{stage}':\n{exc}")
            continue
        finally:
            state.set_doing("idle")
        steps += 1
        if next_stage and next_stage != stage:
            set_stage(slug, next_stage)
            log.info("%s moved %s -> %s", slug, stage, next_stage)
    return steps


async def run_forever(every_seconds: int) -> None:
    log.info("Job runner started (checks every %ss)", every_seconds)
    while True:
        try:
            await run_once()
        except Exception:
            log.exception("Job runner error")
        await asyncio.sleep(every_seconds)

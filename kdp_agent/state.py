"""The agent's own state: paused or not, autonomy level, and what it's doing now."""

from __future__ import annotations

from datetime import datetime, timezone

from . import settings


def load() -> dict:
    state = settings.read_json(settings.STATE_FILE, None)
    if state is None:
        cfg = settings.load_config()
        state = {"paused": False, "level": int(cfg["agent"]["start_level"]),
                 "doing": "idle", "updated_at": None}
        save(state)
    return state


def save(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settings.write_json(settings.STATE_FILE, state)


def update(**changes) -> dict:
    state = load()
    state.update(changes)
    save(state)
    return state


def is_paused() -> bool:
    return bool(load()["paused"])


def set_doing(text: str) -> None:
    update(doing=text)

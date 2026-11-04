"""data/decisions.log - one line per question asked and your answer. Later phases learn from it."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import settings


def log(event: dict) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
    settings.DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def last(n: int = 10) -> list[dict]:
    try:
        lines = settings.DECISIONS_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines[-n:] if line.strip()]

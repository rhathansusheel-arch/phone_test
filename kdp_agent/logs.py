"""One log file per day in logs\\ (agent-YYYY-MM-DD.log), and /logs support."""

from __future__ import annotations

import logging
import sys
from datetime import date

from . import settings


class DailyFileHandler(logging.Handler):
    """Writes to logs/agent-<today>.log and switches to a new file at midnight."""

    def __init__(self):
        super().__init__()
        self._day = None
        self._file = None

    def _open(self):
        today = date.today().isoformat()
        if today != self._day:
            if self._file:
                self._file.close()
            settings.LOGS.mkdir(parents=True, exist_ok=True)
            self._file = open(settings.LOGS / f"agent-{today}.log", "a", encoding="utf-8")
            self._day = today

    def emit(self, record):
        try:
            self._open()
            self._file.write(self.format(record) + "\n")
            self._file.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._file:
            self._file.close()
        super().close()


def setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # emoji in titles never crash the Windows console
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), DailyFileHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def last_lines(n: int = 30) -> str:
    f = settings.LOGS / f"agent-{date.today().isoformat()}.log"
    try:
        return "".join(f.read_text(encoding="utf-8").splitlines(keepends=True)[-n:])
    except FileNotFoundError:
        return ""

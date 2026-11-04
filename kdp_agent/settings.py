"""Folder paths, config.yaml, .env secrets and small file helpers used everywhere."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# The project folder (C:\KDPAgent on your PC). Can be overridden for tests.
ROOT = Path(os.environ.get("KDP_AGENT_ROOT", Path(__file__).resolve().parent.parent))

BOOKS = ROOT / "books"
DATA = ROOT / "data"
INBOX = ROOT / "inbox"
LOGS = ROOT / "logs"
PROMPTS = ROOT / "prompts"
BROWSER_PROFILE = ROOT / "browser_profile"

DECISIONS_LOG = DATA / "decisions.log"
LIMITS_FILE = DATA / "limits.json"
STATE_FILE = DATA / "agent_state.json"


def ensure_folders() -> None:
    """Create every folder the agent uses, if it's missing."""
    for folder in (BOOKS, DATA, INBOX, LOGS, PROMPTS,
                   BROWSER_PROFILE / "kdp", BROWSER_PROFILE / "research"):
        folder.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets() -> tuple[str, int]:
    """Read the bot token and your chat ID from .env. Stops with a clear message if missing."""
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env.\n"
            "Copy .env.example to .env and fill both in (see README step 3)."
        )
    try:
        return token, int(chat_id)
    except ValueError:
        raise SystemExit("TELEGRAM_CHAT_ID in .env must be a number, like 123456789.")


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    """Save safely: write a temp file, then swap it in, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

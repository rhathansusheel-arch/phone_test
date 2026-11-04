"""Run Claude Code in headless mode (`claude -p`) on your Pro plan. No paid API.

If the Pro usage limit is hit, raises UsageLimit with the reset time so the job
runner can save its place and wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone

from . import settings
from .errors import UsageLimit

log = logging.getLogger("claude")

# Only one Claude job at a time, to be gentle on the Pro plan.
_lock = asyncio.Lock()

_LIMIT_PATTERNS = re.compile(
    r"usage limit|limit reached|rate limit|out of (extra )?usage|5-hour limit|weekly limit", re.I)


def _find_command() -> str:
    cmd = settings.load_config()["claude"]["command"]
    found = shutil.which(cmd) or shutil.which(cmd + ".cmd")
    if not found:
        raise RuntimeError(f"Can't find the '{cmd}' command. Is Claude Code installed and on PATH?")
    return found


def _parse_reset(text: str) -> datetime | None:
    # Claude Code prints e.g. "Claude AI usage limit reached|1730000000" or "resets 3pm".
    m = re.search(r"\|(\d{10})", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    return None


async def ask_claude(prompt: str, tools: list[str] | None = None,
                     timeout: float | None = None, read_dir: str | None = None) -> str:
    """Send a prompt to `claude -p` and return its text answer.

    tools: Claude Code tools it may use (e.g. ["WebFetch", "WebSearch"]). None = no tools.
    read_dir: a folder Claude may read files (e.g. page images) from. Turns on the Read tool.
    """
    cfg = settings.load_config()["claude"]
    timeout = timeout or cfg["timeout_seconds"]
    args = [_find_command(), "-p", "--output-format", "json"]
    if read_dir:
        tools = list(tools or []) + ["Read"]
        args += ["--add-dir", str(read_dir)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    else:
        args += ["--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"]
    async with _lock:
        with tempfile.TemporaryDirectory(prefix="kdp_claude_") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"Claude took longer than {timeout:.0f}s and was stopped.")
    text_out = out.decode("utf-8", "replace")
    text_err = err.decode("utf-8", "replace")
    try:
        data = json.loads(text_out)
    except json.JSONDecodeError:
        data = None
    result = (data or {}).get("result", "") if isinstance(data, dict) else ""
    is_error = bool((data or {}).get("is_error")) if isinstance(data, dict) else proc.returncode != 0
    combined = f"{result}\n{text_err}\n{'' if data else text_out}"
    if (is_error or proc.returncode != 0) and _LIMIT_PATTERNS.search(combined):
        raise UsageLimit("Claude usage limit reached", _parse_reset(combined))
    if is_error or proc.returncode != 0:
        raise RuntimeError(f"Claude failed: {combined.strip()[:500]}")
    if _LIMIT_PATTERNS.search(result[:200]) and len(result) < 300:
        raise UsageLimit("Claude usage limit reached", _parse_reset(result))
    return result


def extract_json(text: str):
    """Pull the first JSON object or array out of Claude's answer."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("No JSON found in Claude's answer")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def extract_svg(text: str) -> str:
    m = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
    if not m:
        raise ValueError("No <svg> found in Claude's answer")
    return m.group(0)

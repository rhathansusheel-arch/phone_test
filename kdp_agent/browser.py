"""Browser helpers (Playwright + Chromium).

- One browser session at a time (Hard rule 9).
- Human-paced: 2-5 second pauses between actions.
- Two separate profiles: browser_profile/kdp (your logged-in KDP) and
  browser_profile/research (logged out, for Amazon research only).
- If a login page, a code request or a CAPTCHA appears: stop, screenshot it,
  send it to you and wait until you tap Done. Never type a password, never
  try to solve a CAPTCHA (Hard rule 6).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path

from . import settings
from .errors import NeedsHuman

log = logging.getLogger("browser")

_one_at_a_time = asyncio.Lock()

LOGIN_SIGNS = ["ap/signin", "/ap/mfa", "/ap/cvf", "signin?", "/ap/challenge"]
LOGIN_TEXT = ["Sign in", "Sign-In", "Enter verification code", "Two-Step Verification",
              "One Time Password", "Enter OTP", "Approve the notification"]
CAPTCHA_SIGNS = ["validateCaptcha", "/errors/validateCaptcha", "captcha"]
CAPTCHA_TEXT = ["Enter the characters you see below", "Type the characters you see",
                "Solve this puzzle", "not a robot", "Sorry, we just need to make sure you're not a robot"]


def chromium_path() -> str | None:
    """Optional: a specific Chromium to use (only needed on the build computer)."""
    return os.environ.get("CHROMIUM_PATH") or None


async def human_pause(scale: float = 1.0) -> None:
    b = settings.load_config()["browser"]
    await asyncio.sleep(random.uniform(b["min_pause_seconds"], b["max_pause_seconds"]) * scale)


@asynccontextmanager
async def plain_browser():
    """A throw-away browser with no profile, for drawing/printing local files only (no websites)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chromium_path())
        try:
            yield browser
        finally:
            await browser.close()


@asynccontextmanager
async def profile_session(profile: str):
    """Open the 'kdp' or 'research' profile. Only one at a time across the whole agent."""
    if profile not in ("kdp", "research"):
        raise ValueError("profile must be 'kdp' or 'research'")
    from playwright.async_api import async_playwright
    cfg = settings.load_config()["browser"]
    headless = (not cfg["show_kdp_window"]) if profile == "kdp" else cfg["research_headless"]
    folder = settings.BROWSER_PROFILE / profile
    folder.mkdir(parents=True, exist_ok=True)
    _no_password_saving(folder)
    async with _one_at_a_time:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                str(folder), headless=headless, executable_path=chromium_path(),
                viewport={"width": 1366, "height": 900}, accept_downloads=True,
                locale="en-US")
            try:
                yield ctx
            finally:
                await ctx.close()


def _no_password_saving(folder: Path) -> None:
    """Hard rule 6: switch off Chromium's 'save password?' offer, so a password is never stored."""
    import json
    prefs_file = folder / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8")) if prefs_file.exists() else {}
    except (ValueError, OSError):
        prefs = {}
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    prefs_file.write_text(json.dumps(prefs), encoding="utf-8")


async def page_problem(page) -> str | None:
    """Is this a login, code or CAPTCHA page? Returns what it is, or None."""
    url = page.url.lower()
    try:
        body = await page.inner_text("body", timeout=5000)
    except Exception:
        body = ""
    if any(s.lower() in url for s in CAPTCHA_SIGNS) or any(t.lower() in body.lower() for t in CAPTCHA_TEXT):
        return "a CAPTCHA (robot check)"
    if any(s in url for s in LOGIN_SIGNS):
        return "a login or verification-code page"
    has_password = await page.locator("input[type=password]").count() > 0
    if has_password or (any(t in body for t in LOGIN_TEXT[2:])):
        return "a login or verification-code page"
    return None


async def screenshot(page, name: str) -> Path:
    folder = settings.LOGS / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{name}.png"
    await page.screenshot(path=str(path), full_page=False)
    return path


async def guard(page, where: str, wait_for_human: bool = True) -> None:
    """Call after every page load. Stops at login/code/CAPTCHA and waits for you."""
    from .telegram_bot import ask_me  # imported here to avoid a circular import
    for _ in range(3):
        problem = await page_problem(page)
        if not problem:
            return
        shot = await screenshot(page, "needs-you")
        log.warning("Stopped at %s (%s)", problem, where)
        if not wait_for_human:
            raise NeedsHuman(f"Amazon showed {problem} while {where}.")
        answer = await ask_me(
            f"✋ Amazon is showing {problem} while I was {where}.\n"
            "I never type passwords or solve CAPTCHAs. Please sort it out by hand in the "
            "browser window on your PC, then tap Done.",
            buttons=["Done", "Stop this job"], photos=[shot], about="login-stop")
        if not answer or answer.get("choice") != "Done":
            raise NeedsHuman(f"Stopped at {problem} while {where}.")
        await page.reload()
        await human_pause()
    raise NeedsHuman(f"Still seeing {problem} after 3 tries while {where}.")


async def goto(page, url: str, where: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    await human_pause()
    await guard(page, where)

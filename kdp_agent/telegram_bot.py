"""Your Telegram remote control, plus ask_me() - the one helper every phase uses to ask you things.

Only YOUR chat ID is listened to. Messages from anyone else are ignored.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import decisions, jobs, limits, settings, state

log = logging.getLogger("bot")

_bot = None          # the Telegram bot, set when the agent starts
_chat_id: int | None = None
_pending: dict[str, dict] = {}   # questions waiting for your answer
_ids = itertools.count(1)


# ---------------------------------------------------------------- ask_me

async def ask_me(text: str, buttons: list[str] | None = None,
                 photos: list[str | Path] | None = None,
                 typed_for: list[str] | None = None,
                 about: str | None = None,
                 timeout: float | None = None) -> dict | None:
    """Send you a message (with optional photos and buttons) and wait for your answer.

    text      - the question
    buttons   - button labels, e.g. ["Approve", "Change something"]
    photos    - image files to send first (screenshots, covers, sample pages)
    typed_for - buttons that need you to type more after tapping, e.g. ["Change something"]
    about     - which book/topic this is about (saved in the log)
    timeout   - seconds to wait; None = wait as long as it takes

    Returns {"choice": <button or None>, "text": <what you typed or None>},
    or None if the timeout ran out. Every answer is saved in data/decisions.log.
    You can also just type a reply instead of tapping a button.
    """
    if _bot is None:
        raise RuntimeError("Telegram bot is not running yet.")
    buttons = buttons or []
    typed_for = typed_for or []
    qid = f"{int(asyncio.get_running_loop().time())}-{next(_ids)}"

    if photos:
        await _send_photos([Path(p) for p in photos])

    markup = None
    if buttons:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=f"{qid}|{i}")]
             for i, label in enumerate(buttons)])
    msg = await _bot.send_message(_chat_id, text, reply_markup=markup)

    future = asyncio.get_running_loop().create_future()
    _pending[qid] = {"future": future, "buttons": buttons, "typed_for": typed_for,
                     "choice": None, "awaiting_text": not buttons,
                     "text": text, "about": about, "message_id": msg.message_id}
    decisions.log({"type": "asked", "id": qid, "about": about, "question": text,
                   "buttons": buttons})
    try:
        answer = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        decisions.log({"type": "timeout", "id": qid, "about": about})
        return None
    finally:
        _pending.pop(qid, None)
    decisions.log({"type": "answered", "id": qid, "about": about, "question": text, **answer})
    return answer


async def _send_photos(paths: list[Path]) -> None:
    if len(paths) == 1:
        with open(paths[0], "rb") as f:
            await _bot.send_photo(_chat_id, f)
        return
    for start in range(0, len(paths), 10):  # Telegram allows 10 photos per album
        files = [open(p, "rb") for p in paths[start:start + 10]]
        try:
            await _bot.send_media_group(_chat_id, [InputMediaPhoto(f) for f in files])
        finally:
            for f in files:
                f.close()


async def send_photos(paths: list[str | Path]) -> None:
    """Send pictures to you (no question)."""
    if paths:
        await _send_photos([Path(p) for p in paths])


async def send_files(paths: list[str | Path], caption: str | None = None) -> None:
    """Send files (PDFs etc.) to you as documents."""
    for i, p in enumerate(paths):
        with open(p, "rb") as f:
            await _bot.send_document(_chat_id, f, filename=Path(p).name,
                                     caption=caption if i == 0 else None)


async def handle_button(qid: str, index: int) -> str:
    """You tapped a button. Returns the text to show you."""
    q = _pending.get(qid)
    if q is None or q["future"].done():
        return "This question has expired. I'll ask again if it's still needed."
    label = q["buttons"][index]
    if label in q["typed_for"]:
        q["choice"] = label
        q["awaiting_text"] = True
        return f"You chose: {label}\nPlease type what you'd like."
    q["future"].set_result({"choice": label, "text": None})
    return f"✅ You chose: {label}"


def handle_text(text: str) -> bool:
    """You typed a message. It answers the newest open question. Returns True if it did."""
    open_qs = [q for q in _pending.values() if not q["future"].done()]
    if not open_qs:
        return False
    waiting = [q for q in open_qs if q["awaiting_text"]]
    q = (waiting or open_qs)[-1]
    q["future"].set_result({"choice": q["choice"], "text": text})
    return True


# ---------------------------------------------------------------- commands

HELP = """KDP Agent commands:
/status - what I'm doing now, slots left this week, next reset
/queue - books in progress and their stage
/books - every book, its stage and Amazon link
/pause - stop all work    /resume - start again
/level - autonomy level (/level 1 or /level 2 to change)
/taste - what I've learned about your taste and what sells
/inbox - look for new PDFs in the inbox now
/research - find new book ideas now
/report - this week's sales report now
/adsplan - what the ads step would do (creates nothing)
/stopads - pause all ads    /startads - allow ads again
/hold <book> - put a book on hold    /unhold <book> - continue it
/login - open the KDP window so you can log in by hand
/checkspecs - re-read KDP's rules (sizes, prices) from kdp.amazon.com
/testdraft <book> - fill the paperback on KDP as a draft only (test)
/sample <theme> - draw a 6-page sample (test, never uploaded)
/sales - read KDP Reports now and show the report
/logs - the last 30 lines of today's log
/testask - send a test approval question
/help - this list"""


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = state.load()
    running = "⏸ PAUSED" if s["paused"] else "▶️ Running"
    await update.message.reply_text(
        f"{running}\nDoing now: {s['doing']}\nAutonomy level: {s['level']}\n\n"
        f"Weekly KDP slots:\n{limits.summary()}")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    books = [b for b in jobs.all_books() if b["stage"] != "live"]
    if not books:
        await update.message.reply_text("No books in progress.")
        return
    lines = [f"• {b['title']} - {b['stage']}" + (" ⚠️ " + b["error"] if b.get("error") else "")
             for b in books]
    await update.message.reply_text("Books in progress:\n" + "\n".join(lines))


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state.update(paused=True)
    decisions.log({"type": "command", "command": "pause"})
    await update.message.reply_text("⏸ Paused. I'll stop after the current step. /resume to continue.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state.update(paused=False)
    decisions.log({"type": "command", "command": "resume"})
    await update.message.reply_text("▶️ Resumed.")


async def cmd_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if ctx.args:
        if ctx.args[0] not in ("1", "2"):
            await update.message.reply_text("Level must be 1 or 2. Example: /level 2")
            return
        state.update(level=int(ctx.args[0]))
        decisions.log({"type": "command", "command": "level", "value": int(ctx.args[0])})
    level = state.load()["level"]
    meaning = {1: "you approve every niche and every publish.",
               2: "I pick niches myself and tell you why; you still approve every publish."}
    await update.message.reply_text(f"Autonomy level {level}: {meaning[level]}")


async def cmd_taste(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import sales, taste
    await update.message.reply_text(taste.summary() + "\n\n" + sales.what_sells())


async def cmd_stopads(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import ads
    decisions.log({"type": "command", "command": "stopads"})
    await update.message.reply_text("⏸ Stopping all ads...")
    await update.message.reply_text(await ads.stop_all())


async def cmd_startads(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state.update(ads_stopped=False)
    decisions.log({"type": "command", "command": "startads"})
    on = settings.load_config()["ads"]["enabled"]
    await update.message.reply_text("▶️ Ads allowed again (only under the ₹2,000-profit rule, and I ask before each campaign)."
                                    + ("" if on else "\nNote: ads are still switched off in config.yaml (ads: enabled: false)."))


async def cmd_adsplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import ads
    await update.message.reply_text(ads.dry_run_text())


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import sales
    await update.message.reply_text(sales.weekly_report())


async def cmd_books(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    books = jobs.all_books()
    if not books:
        await update.message.reply_text("No books yet. Put PDFs in the inbox and send /inbox.")
        return
    lines = []
    for b in books:
        line = f"• {b['title']} [{b['slug']}] - {b['stage']}{' (on hold)' if b.get('on_hold') else ''}"
        if b.get("amazon_links"):
            line += "\n  " + b["amazon_links"][0]
        lines.append(line)
    await update.message.reply_text("\n".join(lines)[:4000])


def _find_book(arg: str | None) -> dict | None:
    if not arg:
        return None
    arg = arg.lower()
    for b in jobs.all_books():
        if b["slug"] == arg or arg in b["slug"] or arg in b["title"].lower():
            return b
    return None


async def cmd_hold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    b = _find_book(" ".join(ctx.args))
    if not b:
        await update.message.reply_text("Which book? Example: /hold ocean  (see /books)")
        return
    jobs.update_book(b["slug"], on_hold=True)
    await update.message.reply_text(f"✋ '{b['title']}' is on hold.")


async def cmd_unhold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    b = _find_book(" ".join(ctx.args))
    if not b:
        await update.message.reply_text("Which book? Example: /unhold ocean  (see /books)")
        return
    jobs.update_book(b["slug"], on_hold=False, error=None, wait_until=None)
    await update.message.reply_text(f"▶️ '{b['title']}' continues from stage '{b['stage']}'.")


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import pipeline
    await update.message.reply_text("📥 Looking in the inbox...")
    made = await pipeline.intake_inbox()
    await update.message.reply_text(f"Added {len(made)} book(s)." if made else "No new PDFs in the inbox.")


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import niches
    await update.message.reply_text("🔎 Researching Amazon (slowly, like a person). This takes a while...")
    rows = await niches.research_round(force=True)
    if not rows:
        await update.message.reply_text("No research results (daily research budget used, or Amazon blocked me).")


async def cmd_checkspecs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import specs
    await update.message.reply_text("📏 Reading KDP's help pages. This takes a few minutes...")
    found, diffs = await specs.check_from_kdp()
    text = "My numbers match KDP." if not diffs else "Differences:\n" + "\n".join(diffs[:30])
    a = await ask_me(f"📏 {text}\nSave KDP's numbers?", buttons=["Save", "Don't save"], about="checkspecs")
    if a and a["choice"] == "Save":
        specs.save_checked(found)
        await notify("✅ Saved. KDP numbers confirmed.")


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from .browser import profile_session
    from .kdp_upload import kdp_url
    await update.message.reply_text("🔐 Opening KDP in a browser window on your PC...")
    async with profile_session("kdp") as bctx:
        page = bctx.pages[0] if bctx.pages else await bctx.new_page()
        await page.goto(kdp_url("bookshelf"))
        a = await ask_me("Log in to KDP by hand in that window (including any code Amazon sends). "
                         "I never see or save your password. Tap Done when you see your Bookshelf.",
                         buttons=["Done"], about="login")
    await notify("✅ Thanks - I'll reuse that login." if a else "Login window closed.")


async def cmd_testdraft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import kdp_upload
    b = _find_book(" ".join(ctx.args))
    if not b or b["stage"] != "listed":
        await update.message.reply_text("Send /testdraft <book> for a book at stage 'listed' (see /books).")
        return
    await kdp_upload.ensure_ready()
    r = await kdp_upload.fill_format(b["slug"], "paperback")
    if r.get("queued"):
        await update.message.reply_text(r["why"])
        return
    await send_photos(r["screens"][:10])
    await update.message.reply_text(f"📋 Paperback saved as a DRAFT only (not published). Price ${r['price']:.2f}, "
                                    f"KDP royalty shown: {r['kdp_royalty']}. Warnings: {r['warnings'] or 'none'}")


async def cmd_sample(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 4 test: draw a 6-page sample and send the contact sheet (nothing is uploaded)."""
    from . import artist, pipeline
    theme = " ".join(ctx.args) or "Farm animals"
    slug = jobs.make_slug(f"sample {theme}")
    if not jobs.load_book(slug):
        jobs.create_book(f"Sample: {theme}", slug=slug, on_hold=True)   # on hold: never goes further
        pipeline.save_plan(slug, {"source": "sample", "theme": theme, "audience": "toddlers and preschool kids",
                                  "ages": [2, 5], "seed": f"{theme} coloring book", "picture_count": 6,
                                  "trim": "8.5x11", "formats": []})
    await update.message.reply_text(f"🎨 Drawing a 6-page sample of '{theme}' (about 1 minute per page)...")
    await artist.plan_pages(slug)
    artist.PAGES_PER_RUN = 6
    while not await artist.draw_and_review(slug):
        if not any(p["status"] == "todo" for p in artist.load_pages(slug)["pages"]):
            continue
    await notify("✅ Sample approved. (It's only a sample, so it stays on hold and is never uploaded.)")


async def cmd_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from . import sales
    await update.message.reply_text("📊 Reading KDP Reports...")
    n = await sales.read_kdp_reports()
    await update.message.reply_text(f"Saved {n} sales rows.\n\n" + sales.weekly_report())


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from .logs import last_lines
    await update.message.reply_text(last_lines(30)[-4000:] or "No log lines yet.")


async def cmd_testask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    answer = await ask_me(
        "🧪 Test approval: Pretend this is a book ready to publish. What do you want to do?",
        buttons=["Approve", "Change something"], typed_for=["Change something"],
        about="phase1-test")
    await _bot.send_message(_chat_id, f"Got it. I logged your answer: {answer}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_chat is None or update.effective_chat.id != _chat_id:
        return
    qid, _, index = query.data.partition("|")
    reply = await handle_button(qid, int(index))
    await query.answer()
    try:
        await query.edit_message_reply_markup(None)  # remove the buttons so they can't be tapped twice
        await _bot.send_message(_chat_id, reply)
    except Exception:
        log.exception("Could not update the message")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not handle_text(update.message.text):
        await update.message.reply_text("I'm not waiting for an answer right now. /help shows the commands.")


def _safe(fn):
    """Run a long command; if it fails, tell you instead of going quiet."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await fn(update, ctx)
        except Exception as exc:
            log.exception("Command failed")
            await notify(f"⚠️ That didn't work: {exc}")
    return wrapper


# ---------------------------------------------------------------- start-up

async def notify(text: str) -> None:
    if _bot is not None:
        await _bot.send_message(_chat_id, text)


def build_app(token: str, chat_id: int) -> Application:
    global _chat_id
    _chat_id = chat_id
    me_only = filters.Chat(chat_id=chat_id)
    every = settings.load_config()["agent"]["check_every_seconds"]
    runner: dict = {}

    async def on_start(app: Application) -> None:
        global _bot
        _bot = app.bot
        jobs.notify = notify
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in [
            ("status", "What I'm doing + slots left"), ("queue", "Books in progress"), ("books", "All books"),
            ("pause", "Stop all work"), ("resume", "Start work again"), ("level", "Autonomy level"),
            ("taste", "What I've learned"), ("inbox", "Check the inbox"), ("research", "Find book ideas"),
            ("report", "Weekly report"), ("adsplan", "Ads dry run"), ("stopads", "Pause all ads"),
            ("login", "Log in to KDP by hand"), ("logs", "Last 30 log lines"), ("help", "All commands")]])
        runner["task"] = asyncio.create_task(jobs.run_forever(every))
        await notify("🟢 KDP Agent started. /help shows what I can do.")

    async def on_stop(app: Application) -> None:
        task = runner.get("task")
        if task:
            task.cancel()

    app = Application.builder().token(token).post_init(on_start).post_shutdown(on_stop).build()
    for name, fn in [("help", cmd_help), ("start", cmd_help), ("status", cmd_status),
                     ("queue", cmd_queue), ("pause", cmd_pause), ("resume", cmd_resume),
                     ("level", cmd_level), ("taste", cmd_taste), ("books", cmd_books), ("hold", cmd_hold),
                     ("unhold", cmd_unhold), ("adsplan", cmd_adsplan), ("report", cmd_report),
                     ("startads", cmd_startads), ("logs", cmd_logs)]:
        app.add_handler(CommandHandler(name, fn, filters=me_only))
    # These wait for your tap or take a while, so they must not block the bot from hearing you.
    for name, fn in [("testask", cmd_testask), ("inbox", cmd_inbox), ("research", cmd_research),
                     ("checkspecs", cmd_checkspecs), ("login", cmd_login), ("testdraft", cmd_testdraft),
                     ("stopads", cmd_stopads), ("sample", cmd_sample), ("sales", cmd_sales)]:
        app.add_handler(CommandHandler(name, _safe(fn), filters=me_only, block=False))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(me_only & filters.TEXT & ~filters.COMMAND, on_text))
    return app

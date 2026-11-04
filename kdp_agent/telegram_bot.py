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
/pause - stop all work
/resume - start work again
/level - show the autonomy level (/level 1 or /level 2 to change)
/taste - what I've learned about your taste
/stopads - pause all ads
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
    await update.message.reply_text("Nothing learned yet - this arrives in Phase 5.")


async def cmd_stopads(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    decisions.log({"type": "command", "command": "stopads"})
    await update.message.reply_text("No ads are running. (Ads arrive in Phase 6.)")


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
            ("status", "What I'm doing + slots left"), ("queue", "Books in progress"),
            ("pause", "Stop all work"), ("resume", "Start work again"),
            ("level", "Autonomy level"), ("taste", "What I've learned"),
            ("stopads", "Pause all ads"), ("testask", "Test approval"), ("help", "All commands")]])
        runner["task"] = asyncio.create_task(jobs.run_forever(every))
        await notify("🟢 KDP Agent started. /help shows what I can do.")

    async def on_stop(app: Application) -> None:
        task = runner.get("task")
        if task:
            task.cancel()

    app = Application.builder().token(token).post_init(on_start).post_shutdown(on_stop).build()
    for name, fn in [("help", cmd_help), ("start", cmd_help), ("status", cmd_status),
                     ("queue", cmd_queue), ("pause", cmd_pause), ("resume", cmd_resume),
                     ("level", cmd_level), ("taste", cmd_taste), ("stopads", cmd_stopads)]:
        app.add_handler(CommandHandler(name, fn, filters=me_only))
    # /testask waits for your tap, so it must not block the bot from hearing that tap.
    app.add_handler(CommandHandler("testask", cmd_testask, filters=me_only, block=False))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(me_only & filters.TEXT & ~filters.COMMAND, on_text))
    return app

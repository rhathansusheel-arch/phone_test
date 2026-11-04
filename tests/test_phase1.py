import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.ext import filters

from kdp_agent import decisions, jobs, limits, settings, state, telegram_bot as tb

MY_CHAT = 111


class FakeBot:
    """Pretend Telegram: records what the agent sends."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
def bot():
    tb._bot, tb._chat_id = FakeBot(), MY_CHAT
    tb._pending.clear()
    yield tb._bot


def _qid():
    return next(iter(tb._pending))


# ---------- ask_me

async def test_ask_me_button_tap_is_logged(bot):
    task = asyncio.create_task(tb.ask_me("Publish?", ["Approve", "Cancel"], about="ocean"))
    await asyncio.sleep(0)
    assert bot.sent[0][1] == "Publish?"
    buttons = [b.text for row in bot.sent[0][2].inline_keyboard for b in row]
    assert buttons == ["Approve", "Cancel"]
    reply = await tb.handle_button(_qid(), 0)
    assert reply == "✅ You chose: Approve"
    assert await task == {"choice": "Approve", "text": None}
    log = decisions.last()
    assert [e["type"] for e in log] == ["asked", "answered"]
    assert log[-1]["choice"] == "Approve" and log[-1]["about"] == "ocean"


async def test_ask_me_button_then_typed_reason(bot):
    task = asyncio.create_task(tb.ask_me("Book ok?", ["Approve", "Change something"],
                                         typed_for=["Change something"]))
    await asyncio.sleep(0)
    reply = await tb.handle_button(_qid(), 1)
    assert "Please type" in reply
    assert not task.done()
    assert tb.handle_text("Make the title bigger")
    assert await task == {"choice": "Change something", "text": "Make the title bigger"}


async def test_typed_reply_instead_of_tap(bot):
    task = asyncio.create_task(tb.ask_me("Pick 1, 2 or 3", ["1", "2", "3"]))
    await asyncio.sleep(0)
    assert tb.handle_text("none, too seasonal")
    assert await task == {"choice": None, "text": "none, too seasonal"}


async def test_old_button_after_restart_is_harmless(bot):
    assert "expired" in await tb.handle_button("old-1", 0)


async def test_timeout_returns_none(bot):
    assert await tb.ask_me("Hello?", ["Yes"], timeout=0.05) is None
    assert decisions.last()[-1]["type"] == "timeout"


def test_only_my_chat_is_heard():
    me_only = filters.Chat(chat_id=MY_CHAT)

    def msg_from(chat_id):
        return Update.de_json({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "text": "/status",
            "chat": {"id": chat_id, "type": "private"}}}, None)
    assert me_only.check_update(msg_from(MY_CHAT))
    assert not me_only.check_update(msg_from(999))


def test_bot_builds():
    app = tb.build_app("123:ABC", MY_CHAT)
    commands = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    for c in ("status", "queue", "pause", "resume", "level", "taste", "stopads", "help", "testask",
              "inbox", "research", "report", "adsplan", "hold", "unhold", "login", "checkspecs", "logs", "books"):
        assert c in commands, c


# ---------- weekly limits

def test_week_resets_sunday_midnight_utc():
    wed = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    assert limits.week_start(wed) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert limits.reset_text(wed) == "Sun 08 Nov, 05:30 AM IST"
    sun = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
    assert limits.week_start(sun) == sun


def test_third_paperback_is_refused():
    limits.use_slot("paperback", "created", "ocean")
    limits.use_slot("paperback", "created", "vehicles")
    limits.use_slot("paperback", "created", "ocean")  # same book again: not double-counted
    with pytest.raises(limits.NoSlotLeft):
        limits.use_slot("paperback", "created", "dinosaurs")
    assert limits.slots_left("paperback", "created") == 0
    assert limits.slots_left("hardcover", "created") == 2  # formats are separate


def test_new_week_gives_fresh_slots():
    old = datetime(2026, 11, 4, tzinfo=timezone.utc)
    limits.use_slot("paperback", "created", "a", now=old)
    limits.use_slot("paperback", "created", "b", now=old)
    nxt = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert limits.slots_left("paperback", "created", now=nxt) == 2


# ---------- job runner

async def test_runner_continues_from_saved_stage_after_crash():
    jobs.WORKERS.clear()
    jobs.create_book("Ocean Animals Coloring Book")
    slug = "ocean-animals-coloring-book"

    @jobs.stage_worker("planned")
    async def plan(book):
        return "drawing"

    @jobs.stage_worker("drawing")
    async def crash(book):
        raise RuntimeError("PC switched off")

    await jobs.run_once()            # planned -> drawing
    await jobs.run_once()            # drawing crashes
    saved = jobs.load_book(slug)
    assert saved["stage"] == "drawing" and "PC switched off" in saved["error"]

    jobs.WORKERS.clear()             # "restart" the agent with working code
    jobs.update_book(slug, wait_until=None)   # (after an error the agent waits 30 minutes; skip that)

    @jobs.stage_worker("drawing")
    async def draw(book):
        return "built"

    await jobs.run_once()
    saved = jobs.load_book(slug)
    assert saved["stage"] == "built" and saved["error"] is None
    assert [h["stage"] for h in saved["history"]] == ["planned", "drawing", "built"]
    jobs.WORKERS.clear()


async def test_pause_stops_work():
    jobs.WORKERS.clear()
    jobs.create_book("Vehicles")

    @jobs.stage_worker("planned")
    async def plan(book):
        return "drawing"

    state.update(paused=True)
    assert await jobs.run_once() == 0
    assert jobs.load_book("vehicles")["stage"] == "planned"
    state.update(paused=False)
    assert await jobs.run_once() == 1
    jobs.WORKERS.clear()

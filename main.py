"""Start the KDP Agent: the Telegram bot, the job runner and the timers.

Run:   .venv\\Scripts\\python main.py     (or double-click start.bat)
Stop:  press Ctrl+C in this window
"""

from kdp_agent import jobs, logs, settings, state
from kdp_agent import pipeline, kdp_upload  # noqa: F401  (these register the stage workers)
from kdp_agent import after_launch, niches
from kdp_agent.telegram_bot import build_app


async def inbox_check() -> None:
    """Ask about new PDFs in the inbox (at most once per new file)."""
    seen = settings.read_json(settings.DATA / "inbox_seen.json", [])
    if any(p.name not in seen for p in settings.INBOX.glob("*.pdf")):
        await pipeline.intake_inbox()


def main() -> None:
    logs.setup()
    settings.ensure_folders()
    token, chat_id = settings.load_secrets()
    state.load()
    jobs.PERIODIC[:] = [inbox_check, after_launch.periodic, niches.periodic]
    print("KDP Agent is starting. Leave this window open. Press Ctrl+C to stop.")
    build_app(token, chat_id).run_polling()


if __name__ == "__main__":
    main()

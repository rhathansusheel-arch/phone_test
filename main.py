"""Start the KDP Agent: the Telegram bot and the job runner.

Run:   .venv\\Scripts\\python main.py     (or double-click start.bat)
Stop:  press Ctrl+C in this window
"""

import logging
import sys

from kdp_agent import settings, state
from kdp_agent.telegram_bot import build_app


def setup_logging() -> None:
    settings.LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(settings.LOGS / "agent.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # hide noisy web-request lines


def main() -> None:
    setup_logging()
    settings.ensure_folders()
    token, chat_id = settings.load_secrets()
    state.load()
    print("KDP Agent is starting. Leave this window open. Press Ctrl+C to stop.")
    build_app(token, chat_id).run_polling()


if __name__ == "__main__":
    main()

"""Runs the 7 safety tests in a throw-away folder and sends the checklist to your Telegram.

Run:  .venv\\Scripts\\python safety_check.py      (or double-click safety_check.bat)
Your real books, data and KDP account are never touched.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    from dotenv import dotenv_values
    secrets = dotenv_values(HERE / ".env")
    tmp = Path(tempfile.mkdtemp(prefix="kdp_safety_"))
    shutil.copy(HERE / "config.yaml", tmp / "config.yaml")
    os.environ["KDP_AGENT_ROOT"] = str(tmp)
    sys.path.insert(0, str(HERE))
    from kdp_agent import safety_tests
    results = asyncio.run(safety_tests.run_all())
    text = safety_tests.checklist(results)
    print(text)
    token, chat = secrets.get("TELEGRAM_BOT_TOKEN"), secrets.get("TELEGRAM_CHAT_ID")
    if token and chat:
        from telegram import Bot

        async def send():
            async with Bot(token) as bot:
                await bot.send_message(int(chat), text)
        asyncio.run(send())
        print("\nSent to your Telegram.")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()

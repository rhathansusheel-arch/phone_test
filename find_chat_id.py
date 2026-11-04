"""Find your Telegram chat ID.

1. Put your bot token in .env (TELEGRAM_BOT_TOKEN=...).
2. Run:  .venv\\Scripts\\python find_chat_id.py
3. On your phone, send any message (like "hi") to your new bot.
4. This window prints your chat ID. Copy it into .env as TELEGRAM_CHAT_ID.
"""

import asyncio
import os

from dotenv import load_dotenv
from telegram import Bot

from kdp_agent import settings


async def main() -> None:
    load_dotenv(settings.ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("First put TELEGRAM_BOT_TOKEN=... in the .env file.")
    async with Bot(token) as bot:
        me = await bot.get_me()
        print(f"Connected to your bot @{me.username}.")
        print("Now send it any message from your phone. Waiting...")
        offset = None
        while True:
            for update in await bot.get_updates(offset=offset, timeout=30):
                offset = update.update_id + 1
                chat = update.effective_chat
                if chat:
                    print(f"\nYour chat ID is:  {chat.id}   (from {chat.first_name or chat.title})")
                    print("Copy that number into .env as TELEGRAM_CHAT_ID=... then close this window.")
                    await bot.send_message(chat.id, f"Your chat ID is {chat.id}")
                    return


if __name__ == "__main__":
    asyncio.run(main())

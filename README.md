# KDP Agent

Makes colouring books, writes listings, uploads them to Amazon KDP, and only publishes when you tap **Approve** on Telegram.
The rulebook is in `CLAUDE.md`. (A full beginner guide comes in Phase 7.)

## First-time setup on your Windows PC

**1. Check Python and Git.** Open the Windows terminal (press Start, type `cmd`, press Enter) and type:
```
py -3.12 --version
git --version
```
If either says "not recognized" or "not found":
- Python 3.12: https://www.python.org/downloads/windows/ → pick the newest **Python 3.12.x**, "Windows installer (64-bit)". During install, tick **"Add python.exe to PATH"**.
- Git: https://git-scm.com/download/win → run the installer and click Next on every screen.
Then close the terminal and open a new one.

**2. Get the code into `C:\KDPAgent`** (your `inbox` folder and PDFs stay untouched):
```
cd C:\KDPAgent
git init
git remote add origin https://github.com/rhathansusheel-arch/phone_test.git
git pull origin ccr-2d037c59-qktmwl
```
If a GitHub login window pops up, log in. To get updates after each new phase, run only the last line again.

**3. Install the agent's packages** — double-click `setup.bat` in `C:\KDPAgent`. It also creates your `.env` file.

**4. Make your Telegram bot**
1. In Telegram, search for **@BotFather** (blue tick) and open it.
2. Send `/newbot`. Give a name, e.g. `Rhaths KDP Agent`.
3. Give a username ending in `bot`, e.g. `rhaths_kdp_bot`.
4. BotFather replies with a **token** like `123456789:ABCdef...`. Keep it secret.
5. Open `C:\KDPAgent\.env` in Notepad. After `TELEGRAM_BOT_TOKEN=` paste the token. Save.

**5. Find your chat ID.** In the terminal:
```
cd C:\KDPAgent
.venv\Scripts\python find_chat_id.py
```
Then send "hi" to your new bot. The terminal shows your chat ID number. Paste it after `TELEGRAM_CHAT_ID=` in `.env`. Save.

## Start, stop, test
- **Start:** double-click `start.bat`. Your bot says "🟢 KDP Agent started". Leave the black window open.
- **Stop:** click the black window and press **Ctrl+C** (or just close it).
- **Pause work but keep the bot listening:** `/pause` in Telegram, then `/resume`.
- **Check:** `/status` (what it's doing, KDP slots left, next reset) and `/queue` (books and stages).
- **Test approval:** send `/testask`, tap a button. Your answer is saved in `data\decisions.log`.

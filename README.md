# KDP Agent

Your helper that makes colouring books, writes their Amazon listings, uploads them to KDP as drafts,
and **only publishes when you tap Publish on Telegram**. The full rulebook is in `CLAUDE.md`.

---

## 1. First-time setup (do this once)

**1.1 Check Python and Git.** Press Start, type `cmd`, press Enter. In the black window type:
```
py -3.12 --version
git --version
```
If either says "not recognized":
- Python 3.12: https://www.python.org/downloads/windows/ → newest **Python 3.12.x** → "Windows installer (64-bit)".
  On the first install screen tick **"Add python.exe to PATH"**, then click Install Now.
- Git: https://git-scm.com/download/win → run it, click Next on every screen.

Close the black window and open a new one.

**1.2 Get the code into `C:\KDPAgent`** (your `inbox` folder and PDFs stay as they are):
```
cd C:\KDPAgent
git init
git remote add origin https://github.com/rhathansusheel-arch/phone_test.git
git pull origin ccr-2d037c59-qktmwl
```
If a GitHub login window pops up, log in.

**1.3 Install** - double-click **`setup.bat`**. It installs the Python packages and a private Chromium browser
into `C:\KDPAgent` only (about 5-10 minutes, ~500 MB).

**1.4 Make your Telegram bot**
1. In Telegram search **@BotFather** (blue tick), open it, send `/newbot`.
2. Name it (e.g. `Rhaths KDP Agent`), then a username ending in `bot` (e.g. `rhaths_kdp_bot`).
3. Copy the **token** it gives you. Open `C:\KDPAgent\.env` with Notepad, paste it after `TELEGRAM_BOT_TOKEN=`, save.
4. In the black window: `cd C:\KDPAgent` then `.venv\Scripts\python find_chat_id.py`.
   Send "hi" to your new bot. Paste the number it shows after `TELEGRAM_CHAT_ID=` in `.env`, save.

**1.5 Start it** - double-click **`start.bat`**. Your bot says "🟢 KDP Agent started".

**1.6 Log in to KDP once** - send `/login` to your bot. A browser window opens on your PC. Log in to KDP by hand
(including any code Amazon sends you), then tap **Done** in Telegram. The agent reuses that login. It never sees
or saves your password.

**1.7 Your 3 PDFs** - they're in `C:\KDPAgent\inbox`. Send `/inbox` and answer which book each PDF is.

---

## 2. Every day

| You want to... | Do this |
| --- | --- |
| Start the agent | Double-click `start.bat` (or it starts by itself after step 4) |
| Stop the agent | Close the black "KDP Agent" window |
| Pause work (bot still listens) | `/pause`, later `/resume` |
| See what it's doing | `/status` (also shows KDP slots left and the next reset time in IST) |
| See books and their stage | `/queue` or `/books` |
| Stop one book | `/hold ocean`, later `/unhold ocean` |

Anything that needs you arrives as a Telegram message with buttons.

## 3. What happens each week

1. **Ideas** - when fewer than 2 books are getting ready, it researches Amazon and sends you 3 ideas. Tap 1, 2, 3 or None (say why).
2. **Drawing** - Claude draws every page; you get a contact sheet. Tap Approve all, or Redo pages (type numbers + why).
3. **Checking + building** - interior PDF (title page, "belongs to" page, copyright page, pictures with blank backs, colour test page) and wrap covers.
4. **Review** - you get the cover, 3 sample pages and the listing with 2 prices. Tap Approve A/B or Change something.
5. **Upload** - it fills KDP (paperback, then hardcover) and clicks **Save as Draft only**.
6. **Publish** - you get screenshots of every KDP page. Tap **Publish**, Fix something, or Cancel.
7. **After launch** - it checks twice a day until the book is live and sends the Amazon link. Then: daily sales,
   Sunday-evening report, suggestions for slow sellers (you approve), and ads only under your ₹2,000 rule.

Your approvals, rejections and reasons are saved in `data\taste.json`. `/taste` shows what it has learned.
After 5 niche approvals in a row it will suggest `/level 2` (it picks niches itself) - only you can change the level.

## 4. Auto-start when you log in

Easy way: double-click **`install_autostart.bat`** once. (To undo: `schtasks /Delete /TN "KDP Agent" /F`.)

By hand, if you prefer:
1. Press Start, type **Task Scheduler**, open it.
2. On the right click **Create Basic Task...**
3. Name: `KDP Agent` → Next.
4. Trigger: **When I log on** → Next.
5. Action: **Start a program** → Next.
6. Program/script: click Browse and pick `C:\KDPAgent\start.bat`. In "Start in" type `C:\KDPAgent` → Next → Finish.

`start.bat` restarts the agent by itself 30 seconds after any crash.

**Keep the PC awake while it works:** Start → Settings → System → **Power & battery** (Windows 11) or
**Power & sleep** (Windows 10) → under "Sleep", set **"When plugged in, put my device to sleep after"** to **Never**.
The screen may still turn off - that's fine. Keep the laptop plugged in.

## 5. All Telegram commands

`/status` `/queue` `/books` `/pause` `/resume` `/level` `/taste` `/inbox` `/research` `/report` `/sales`
`/adsplan` `/stopads` `/startads` `/hold` `/unhold` `/login` `/checkspecs` `/testdraft` `/sample` `/logs`
`/testask` `/help` - send `/help` to see what each one does.

## 6. Testing it (before you trust it)

- **All automatic tests:** double-click `run_tests.bat` (a few minutes; no KDP or Amazon needed).
- **The 7 safety tests:** double-click `safety_check.bat` - the checklist arrives on Telegram.
  (Weekly limit, AI disclosure = Yes, trademark filter, login/CAPTCHA stop, locked title, ads rule, restart.)
  They run on a pretend KDP website and never touch your real account.
- **Phase tests on the real thing:** `/checkspecs` (KDP's rules), `/sample Farm animals` (6-page drawing test),
  `/research` (3 ideas), `/testdraft ocean` (paperback saved as a DRAFT only), `/sales` + `/adsplan`.

## 7. Where things are

| Folder / file | What's inside |
| --- | --- |
| `inbox\` | PDFs you give the agent; `inbox\reports\` for KDP report files you download yourself |
| `books\<name>\` | Each book: pages, interior and cover PDFs, `plan.json`, `listing.json`, `status.json`, screenshots |
| `data\` | `taste.json`, `niches.csv`, `sales.csv`, `limits.json`, `decisions.log`, `ads.csv` |
| `logs\` | One log file per day (`/logs` sends the last 30 lines); `logs\screenshots\` |
| `backups\` | A copy of `data\` and every book's listing/status, made daily, kept 30 days |
| `config.yaml` | All settings, explained in simple English |
| `prompts\style_*.md` | The drawing style guides - edit them to change how pages look |

## 8. When something breaks

- **The bot doesn't answer:** is the black "KDP Agent" window open? If not, double-click `start.bat`.
  If it closes straight away, open `logs\` and read the newest file's last lines (or send them to Claude Code).
- **"Amazon is showing a login / CAPTCHA":** go to the browser window on your PC, sort it out by hand, tap Done.
  The agent never types passwords or solves CAPTCHAs.
- **"I couldn't find the field ..." (🛑):** KDP changed its website. Nothing was published. Open Claude Code in
  `C:\KDPAgent`, tell it the message, and it will update the field names in `kdp_agent\kdp_upload.py`.
  Then `/unhold <book>`.
- **"Claude usage limit reached":** nothing to do - it saved its place and continues after the reset.
- **A book is stuck:** `/books` shows it; `/unhold <book>` makes it try again.
- **Get the newest version of the agent:** close the agent window, then in `C:\KDPAgent` run
  `git pull origin ccr-2d037c59-qktmwl`, then `setup.bat`, then `start.bat`.

## 9. Ads

Ads are **off** until you switch them on in `config.yaml` (`ads: enabled: true`) - we'll test them together first.
Even then: only for books with ₹2,000+ profit, at most 30% of that book's profit, never above the daily cap,
and you approve every new campaign. `/adsplan` shows what it would do; `/stopads` pauses everything.

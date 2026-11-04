# KDP Agent — Rulebook

> Claude Code reads this file at the start of every session. Follow every rule here.

## Who I'm working with
- A beginner (B.Com student, not technical).
- Explain every step simply. Say exactly what to type or click.
- **Ask before installing anything.**
- Before each build phase: explain in simple words what we're building and list what's needed from the user (a login, a token, a file, a click).
- After each phase: run its test, show the result, and wait for an OK before moving on.

## Goal
Keep publishing good colouring books on the user's Amazon KDP account under the pen name **"Rhaths"**, with little work from the user, **without ever risking the account**.

## How it runs
- **Machine:** Windows PC, Python 3.12, folder `C:\KDPAgent` (not inside OneDrive). Starts when the user logs in and runs in the background.
- **Brain:** Claude Code headless mode (`claude -p`) on the user's Claude Pro plan. **No paid API.** When a usage limit is hit: save progress, wait for the reset, then continue.
- **Control:** a new Telegram bot, just for KDP. Anything that needs the user arrives as a Telegram message with buttons.
- **Browser work:** Playwright + Chromium.
  - `browser_profile\kdp` = the user's logged-in KDP session (user logs in once by hand)
  - `browser_profile\research` = a separate, logged-out profile for Amazon research
- **Data in plain files:**
  - `books\<slug>\` = pages, interior PDFs, covers, `plan.json`, `listing.json`, `status.json`
  - `data\` = `taste.json`, `niches.csv`, `sales.csv`, `limits.json`, `decisions.log`
  - `config.yaml` = all settings, with comments in simple English

## Pipeline for each book
1. **Niche:** research Amazon (search suggestions + top 10 competitors). Send the user 3 ideas on Telegram.
2. **Plan:** trim size, picture count and style, copied from top sellers in that niche. Page list with no repeats.
3. **Draw:** Claude writes each page as SVG line art. Automatic quality checks.
4. **Interior PDF:** title page, "This book belongs to" page, copyright page, one picture per right-hand page with blank backs, test colour page.
5. **Covers:** paperback and hardcover wrap covers sized to KDP's spec for the exact page count.
6. **SEO listing:** using the `kdp-listing-seo` rules (Phase 2).
7. **Price:** match top competitors, above KDP's minimum, royalty shown.
8. **Upload:** fill KDP for paperback, then hardcover. **Stop before Publish.** Send screenshots on Telegram. Publish only when the user taps **Approve**.
9. **After launch:** daily sales log, weekly report, slow-seller fixes, learning, ads (see Ads rule).

Book stages (saved in `status.json`): `planned → drawing → built → listed → drafted → waiting_approval → in_review → live`.
Weekly loop: research niches → user picks niche → draw → build PDF + covers → write listing → fill KDP as draft → user approves → publish → track sales → learn → back to research.
If the user rejects a niche or asks for a change, go back a step and save the reason.

## First jobs
Three interior PDFs made in Claude Design are in `C:\KDPAgent\inbox`:
- Ocean & sea-life — kids ages 2–5, 40 pictures
- Vehicles — kids ages 4–8, 40 pictures
- Bold & Easy Christmas — adults and seniors

Check them, fix them for KDP, make covers and listings, upload.
The user may upload Christmas by hand first (seasonal timing) — **ask them**.

## Learning and autonomy
- Save every approval, rejection and the user's reason in `data\taste.json`.
- **Level 1 (start):** user approves every niche and every publish.
- **Level 2:** after 5 books in a row where the user approves the niche unchanged, the agent picks niches itself and says why. It **still asks before every publish.**
- Only the user changes the level, with `/level` on Telegram.
- Use sales data (which themes, ages, sizes, prices sell) to rank future niches.

## Hard rules (never break)
1. **KDP weekly limit:** max 2 new titles **created** AND max 2 **submitted**, per format (paperback, hardcover), per week. Reset Sunday 00:00 UTC (5:30 AM IST). Saving a draft counts as created — never make spare drafts. Track in `data\limits.json` and never exceed it. Updates to existing books don't count.
2. **AI disclosure:** always answer KDP's AI-generated content question honestly — **Yes** for images (tool: Claude by Anthropic), and **Yes** for text if Claude wrote it.
3. **No trademarks, characters or brands** (Disney, Bluey, Pokemon, Crayola, etc.) anywhere: pages, covers, titles, keywords.
4. **Never copy** competitors' titles, art or descriptions.
5. **No near-duplicate books:** each new book must clearly differ in theme or audience from existing books.
6. **Never solve or bypass a CAPTCHA, and never store the password.** If Amazon shows a login, code or CAPTCHA: pause and message the user.
7. **Never change a live book's title, subtitle or author** (KDP locks them). Only description, keywords, categories and price.
8. **Never spend money** except under the Ads rule.
9. **Browse slowly like a person:** 2–5 second pauses, one browser session at a time.
10. **If anything is new, unclear or risky, ask on Telegram before doing it.**

## Ads rule
- A book's profit = its royalties minus its ad spend, in rupees.
- Ads may start for a book only after its profit reaches **Rs 2,000**.
- Then spend at most **30% of that book's profit**, with a daily cap from `config.yaml`. Sponsored Products only.
- Message the user when a campaign starts. `/stopads` pauses all campaigns at once.

## Build phases
0. Blueprint — this file
1. Setup + Telegram bot
2. PDF checker, cover maker, SEO listing
3. KDP uploader with Telegram approval
4. Colouring page artist (SVG)
5. Niche research + taste learning
6. Sales reports, slow-seller fixes, Amazon Ads
7. Run 24/7 + safety tests + README

## Build progress
- [x] Phase 0 — CLAUDE.md written (2026-11-04)
- [x] Phase 1 — code written and tested in the cloud with a pretend Telegram (2026-11-04). Waiting for user to run it on the PC (README steps 1-5) and confirm /testask works.
- [ ] Phase 2 — not started
- Setup: user chose Option B — code is written in the cloud repo (rhathansusheel-arch/phone_test, branch ccr-2d037c59-qktmwl); user pulls it into C:\KDPAgent with `git pull` and runs it there.
- Code map: main.py (start), kdp_agent/telegram_bot.py (commands + ask_me), kdp_agent/jobs.py (stages + runner, add workers with @stage_worker), kdp_agent/limits.py (weekly slot guard), kdp_agent/state.py, kdp_agent/decisions.py, tests/ (run: python -m pytest).
- Note: KDP changed its limits twice in autumn 2026 — recheck numbers on kdp.amazon.com before relying on them.

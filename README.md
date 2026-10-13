# VHS Sprachtest watcher

Checks the [Berlin VHS course search](https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx)
every 5 minutes and sends you a Telegram message when a **Sprachtest zur Einbürgerung** date has a free place:
either a full date where a place opened up, or a newly published date that still has places.
When nothing is free, it stays quiet.

## How it works

- `vhs_watch.py` searches for "Einbürgerung" (as if typed into the course search), reads every
  result page and keeps the courses whose title contains "Sprachtest".
- It compares the result with `state.json` from the previous check and messages you about dates
  that have become bookable. A date that fills up and later frees up again is reported again.
- GitHub Actions (`.github/workflows/check.yml`) runs it every 5 minutes and commits `state.json` when
  something changed, so the repository history doubles as a log.

## Setup (once)

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Install the GitHub CLI and log in: `brew install gh`, then `gh auth login`.
3. Run `python3 setup_bot.py`. It connects the bot, creates the public repository, saves the
   token and chat ID as secrets and starts the first check.

## Automatic booking

Once `VHS_EMAIL` and `VHS_PASSWORD` are saved as secrets (run `python3 setup_booking.py` once; it
logs in, stops before booking and saves them), the bot books the earliest newly bookable date with
your VHS account: course page → basket → checkout → login → "Kostenpflichtig buchen". It clicks
only if the summary lists exactly that one date, and you get a Telegram message with the result.

- It books at most once. After the click it writes `"auto_booking_done": true` to `state.json` and
  only sends alerts from then on. Delete that line to allow another automatic booking; delete the
  `VHS_PASSWORD` secret to switch booking off.
- Test the login and booking steps without booking anything:
  `gh workflow run check.yml -f booking_check=true`.
- A booking is binding: the fee is taken by direct debit. Cancelling is free by e-mail within 14 days
  of booking, but not possible from 13 days before the exam.

## Day to day

- Check now: `gh workflow run check.yml` (or Actions → Check Sprachtest dates → Run workflow).
- Pause or resume: `gh workflow disable check.yml` / `gh workflow enable check.yml`.
- Try it locally without sending anything: `python3 vhs_watch.py --dry-run`.
- Watch something else: set the repository variables `SEARCH_TERM` (the text you'd type into the
  course search) and `TITLE_FILTER` (a regex matched against course titles), for example
  `gh variable set SEARCH_TERM --body "Einbürgerungstest"`.
- If a check can't read the site, that run fails and GitHub emails you.
- Cost: none. The repository is public, and GitHub Actions minutes are free for public repositories.
  (Checking every 5 minutes uses about 8,600 minutes a month, far more than the 2,000 free minutes
  a private repository gets.) The bot token and chat ID are secrets and stay hidden.

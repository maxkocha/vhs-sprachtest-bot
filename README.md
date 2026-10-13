# VHS Sprachtest watcher

Checks the [Berlin VHS course search](https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx)
every hour and sends you a Telegram message when a **Sprachtest zur Einbürgerung** date has a free place:
either a full date where a place opened up, or a newly published date that still has places.
When nothing is free, it stays quiet.

## How it works

- `vhs_watch.py` searches for "Einbürgerung" (as if typed into the course search), reads every
  result page and keeps the courses whose title contains "Sprachtest".
- It compares the result with `state.json` from the previous check and messages you about dates
  that have become bookable. A date that fills up and later frees up again is reported again.
- GitHub Actions (`.github/workflows/check.yml`) runs it every hour and commits `state.json` when
  something changed, so the repository history doubles as a log.

## Setup (once)

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Install the GitHub CLI and log in: `brew install gh`, then `gh auth login`.
3. Run `python3 setup_bot.py`. It connects the bot, creates the private repository, saves the
   token and chat ID as secrets and starts the first check.

## Day to day

- Check now: `gh workflow run check.yml` (or Actions → Check Sprachtest dates → Run workflow).
- Pause or resume: `gh workflow disable check.yml` / `gh workflow enable check.yml`.
- Try it locally without sending anything: `python3 vhs_watch.py --dry-run`.
- Watch something else: set the repository variables `SEARCH_TERM` (the text you'd type into the
  course search) and `TITLE_FILTER` (a regex matched against course titles), for example
  `gh variable set SEARCH_TERM --body "Einbürgerungstest"`.
- If a check can't read the site, that run fails and GitHub emails you.
- Cost: each run counts as one minute of GitHub Actions time, about 720 a month, within the 2,000
  free minutes that GitHub Free includes for private repositories.

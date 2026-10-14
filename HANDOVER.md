# Handover: VHS Sprachtest bot

Built on 13–14 October 2026 with Claude Code. This file is for whoever turns the bot on again,
person or AI: what it is, how it is set up, what was learned, and how to restart it.

## Status (14 October 2026): switched off

The bot made its one automatic booking on 14 October 2026. Then it was switched off:

- The workflow `check.yml` is **disabled**, so there are no scheduled or manual runs.
- The secrets `VHS_EMAIL` and `VHS_PASSWORD` were **deleted**. `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` are still set.
- `state.json` contains `"auto_booking_done": true`. Even when switched on again, the bot only
  sends alerts until that line is removed.
- cron-job.org (see README) was never set up, so there's nothing to cancel there.

## What it does

On each run, in GitHub Actions in this public repository:

1. Searches the Berlin VHS course list (vhsit.berlin.de) for "Einbürgerung".
2. Keeps courses whose title contains "Sprachtest" (the *Sprachtest zur Einbürgerung*).
3. Compares them with `state.json`.
4. Sends a Telegram message when a date has become bookable: a place opened up, or a new date
   appeared with places.

With the VHS login saved as secrets, it also books the earliest newly bookable date with that
account, at most once, and messages the result.

## Files

| File | Purpose |
|---|---|
| `vhs_watch.py` | The check and the booking. Python 3.9+, standard library only. |
| `setup_bot.py` | One-time Telegram setup. Asks for the BotFather token (hidden), waits for a message to the bot to learn the chat ID, creates or reuses this repo, saves the secrets and starts a run. Safe to run again. |
| `setup_booking.py` | One-time VHS login setup. Asks for the login (hidden) and walks the booking steps with a *full* date up to, but not including, "Kostenpflichtig buchen". Then it empties the basket and saves `VHS_EMAIL`/`VHS_PASSWORD`. |
| `.github/workflows/check.yml` | Schedule (`*/5`), manual runs with a `booking_check` input, and commits of `state.json`/`search_form.json` when they change. |
| `state.json` | The courses seen at the last check, plus `auto_booking_done`. |
| `search_form.json` | The search page's saved form, so a check can skip loading that 0.5 MB page. |
| `README.md` | User guide, including the cron-job.org setup for real 5-minute checks. |

## Turn it on again

You need the GitHub CLI, logged in as `maxkocha` (`brew install gh`, then `gh auth login`).

1. **Alerts only.** Enable the workflow, start one run, and check the run log for
   "Search 'Einbürgerung': N matching courses…":
   ```bash
   gh workflow enable check.yml --repo maxkocha/vhs-sprachtest-bot
   gh workflow run check.yml --repo maxkocha/vhs-sprachtest-bot
   ```
2. **New Telegram bot or chat.** Run `gh repo clone maxkocha/vhs-sprachtest-bot`, then
   `python3 setup_bot.py` in a terminal from the clone.
3. **Automatic booking again.** This books one date: a binding booking, with the fee taken by
   direct debit.
   1. The VHS online account needs the SEPA direct-debit mandate on file. Without it, an online
      booking is only held for 5 days.
   2. In the clone, run `python3 setup_booking.py` in a terminal. The login is typed into hidden
      prompts, and the script saves it as secrets.
   3. Remove the `"auto_booking_done": true` line from `state.json`, then commit and push.
   4. Test without booking:
      `gh workflow run check.yml --repo maxkocha/vhs-sprachtest-bot -f booking_check=true`.
      The log should say "Booking check passed".
   5. Fix the known issue below first.
4. **Real 5-minute checks.** GitHub starts `*/5` schedules only about every 15–30 minutes. Use
   cron-job.org; see "Reliable 5-minute checks" in the README.

## Known issues

- **"Waitlist" was reported for a real booking.** After the real booking, the bot reported
  "waitlist", but a place had been booked. The page after "Kostenpflichtig buchen" apparently
  contains the word "Warteliste", and `book()` treats that as a waitlist result. Before relying
  on its messages, make that check stricter, for example by looking for the actual confirmation
  wording. The VHS confirmation email is what counts.
- **No automated tests in the repo.** Check changes with `python3 vhs_watch.py --dry-run` and
  with the booking check above.
- **Claude Code's auto mode blocks the booking steps.** Its safety filter stops anything that
  logs in or books. Use manual approval mode for that work.

## What was learned about the VHS site

- **Shared links don't work.** A `CourseList.aspx` link only works inside the browser session
  that ran the search. The bot runs the search itself: a POST to `CourseSearch.aspx` with the
  field `ctl00$Content$SimpleSearch1$SimpleSearchBox$txtSearchTerm` and the button
  `ctl00$Content$btnSearch`.
- **Pages:**
  - The site uses ISO-8859-15 encoding and shows 10 results per page.
  - Each page load takes about 10 seconds, whatever its size.
  - The "freie Plätze" column shows `belegt` (full), a number, `> 2`, or nothing (already
    started or not bookable).
- **Parallel loading:**
  - The result list lives in the server session, so each page loaded in parallel needs its own
    search.
  - The search form's ViewState can be reused across sessions.
  - When the site refuses it, it answers HTTP 500 "Validation of viewstate MAC failed". The bot
    then saves the form afresh, and as a last resort lets each session load the search page.
  - A check takes about 22 seconds: page 1 after about 11 seconds, pages 2 and 3 after about 22.
- **Booking path:**
  1. The basket icon on the result row (`btnAddToBasketActive`), or "In den Warenkorb" on
     `CourseDetail.aspx?id=…`.
  2. The side menu's "Warenkorb", then "Zur Kasse gehen" (`ctl00$Content$btnBookingSummary`).
  3. "Ich möchte mich einloggen und buchen": `rdbLoginAndBook`, e-mail, password, "Weiter".
  4. The "Zusammenfassung" page, then "Kostenpflichtig buchen"
     (`ctl00$Content$ctl00$btnOnlineBooking`).
  
  There's no terms checkbox. The bot leaves the newsletter box and "Offline-Anmeldung" alone,
  and clicks only when the summary lists exactly that one date and the basket count is 1.
- **No guest booking.** A booking without an account ("ohne Registrierung") isn't binding
  right away, because staff process it later.
- **Site bugs:**
  - Opening an empty basket returns HTTP 500.
  - Emptying the basket works ("Warenkorb leeren", then "Ja" = `ctl00$Content$btnOk`, not
    `btnMsgDeleteBasket`, which belongs to the wish-list dialog), but it answers HTTP 500.
  - The bot reads the basket from the side menu's "Warenkorb (N)" instead.
- **Cancelling** is free by e-mail within 14 days of booking, but impossible from the 13th day
  before the exam.

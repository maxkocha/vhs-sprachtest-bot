#!/usr/bin/env python3
"""One-time setup for automatic booking with your VHS account. Run it in a terminal.

It asks for your VHS e-mail address and password (the password stays hidden), then
goes through the booking steps for one full Sprachtest date: course page, basket,
checkout, login. It stops on the page after the login, before anything could be
booked, and writes an outline of that page to booking_outline.txt: its buttons,
check boxes and sentences about the terms, without form values, with e-mail
addresses and IBANs masked. Then it empties the basket again and, if the login
worked, saves the e-mail address and password as GitHub secrets for the bot.
"""

import getpass
import os
import re
import sys

import vhs_watch as w
from setup_bot import check_gh, run

OUTLINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booking_outline.txt")


def pick_full_date():
    """A date that is full but takes waitlist sign-ups, so practising can't take anyone's place."""
    for course in w.fetch_with_retries(w.DEFAULT_SEARCH_TERM):
        if (re.search(w.DEFAULT_TITLE_FILTER, course["title"], re.IGNORECASE) and course["id"]
                and not course["available"] and "warteliste" in course["hint"].lower()):
            return course
    sys.exit("There's no full Sprachtest date to practise with right now. Try again later.")


def main():
    check_gh()
    email = input("VHS account e-mail address: ").strip()
    password = getpass.getpass("VHS password (nothing shows while you type): ")
    print("Looking up a full date to practise with…")
    course = pick_full_date()
    print(f"Practising with {w.pretty_begin(course['begin'])}, {course['district']} ({course['number']}). Nothing gets booked.")
    report = [f"practice date: {course['number']} {course['begin']} places={course['places']!r}"]

    session = w.Session()
    page = None
    logged_in = False
    try:
        page = w.put_in_basket(session, course)
        report.append("put in basket: ok")
        print("Put it in the basket. Going to checkout and logging in…")
        page = w.checkout_and_login(session, page, email, password)
        logged_in = True
        report.append("login: ok\n--- page after logging in (the bot would book from here):\n" + w.describe_page(page))
        print("Logged in, and stopped there.")
    except (w.BookingError, OSError) as e:
        report.append(f"stopped: {e}")
        if page is not None:
            report.append("--- page where it stopped:\n" + w.describe_page(page))
        print(f"Stopped: {e}")

    if page is not None:
        try:
            emptied = w.empty_basket(session, page)
        except (w.BookingError, OSError):
            emptied = False
        report.append(f"basket emptied: {emptied}")
        print("Emptied the basket again." if emptied else
              "Couldn't empty the basket. Nothing was booked, but you may want to remove the date from your basket on the VHS site.")

    saved = False
    if logged_in:
        saved = all(run("gh", "secret", "set", name, input=value, check=False).returncode == 0
                    for name, value in (("VHS_EMAIL", email), ("VHS_PASSWORD", password)))
        print("Saved your VHS login as GitHub secrets." if saved else "Couldn't save the GitHub secrets.")
    report.append(f"secrets saved: {saved}")

    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nWrote the page outline to {os.path.basename(OUTLINE_FILE)}. Go back to the chat and say it's done.")
    if not logged_in:
        sys.exit(1)


if __name__ == "__main__":
    main()

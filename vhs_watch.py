#!/usr/bin/env python3
"""Check the Berlin VHS course list and send a Telegram message when a
"Sprachtest zur Einbürgerung" date has free places.

Each run does one check and exits; GitHub Actions runs it every 5 minutes. A message
is sent only when a date is bookable now that wasn't at the previous check
(a place opened up, or a new date appeared with places free). What was seen is
remembered in state.json. Only the Python standard library is used.

Environment variables:
  TELEGRAM_BOT_TOKEN  bot token from @BotFather
  TELEGRAM_CHAT_ID    chat to notify (several can be given, comma-separated)
  SEARCH_TERM         text typed into the VHS course search (default: Einbürgerung)
  TITLE_FILTER        regex; only courses whose title matches are watched (default: Sprachtest)
  STATE_FILE          where to keep the last result (default: state.json next to this file)

Usage:
  python3 vhs_watch.py            check and notify
  python3 vhs_watch.py --dry-run  print what would be sent; don't send or save anything
"""

import argparse
import datetime
import html
import http.client
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE_URL = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/"
SEARCH_URL = BASE_URL + "CourseSearch.aspx"
LIST_URL = BASE_URL + "CourseList.aspx"
DETAIL_URL = BASE_URL + "CourseDetail.aspx?id={}"
SITE_ENCODING = "iso-8859-15"  # what the server declares in its Content-Type header
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
SEARCH_BOX = "ctl00$Content$SimpleSearch1$SimpleSearchBox$txtSearchTerm"
SEARCH_BUTTON = ("ctl00$Content$btnSearch", "Suchen")
NO_RESULTS = "Zu Ihrer Suche wurden keine Kurse gefunden"
PAGE_DELAY = 1.5  # seconds between result pages; the site throttles fast paging
MAX_PAGES = 30
MAX_MESSAGE_LEN = 4000  # Telegram's limit is 4096 characters

DEFAULT_SEARCH_TERM = "Einbürgerung"
DEFAULT_TITLE_FILTER = "Sprachtest"


class ScrapeError(Exception):
    pass


class TelegramError(Exception):
    pass


# --- VHS website ------------------------------------------------------------


class FormParser(HTMLParser):
    """Collects the fields a browser would submit with the page's ASP.NET form,
    and the name of the result pager's "next page" button if it is enabled."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.next_button = None
        self._in_form = False
        self._select = None
        self._options = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "aspnetForm":
            self._in_form = True
        if not self._in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = (a.get("type") or "text").lower()
            if kind in ("hidden", "text"):
                self.fields[name] = a.get("value") or ""
            elif kind in ("checkbox", "radio") and "checked" in a:
                self.fields[name] = a.get("value") or "on"
            elif (
                kind == "image"
                and "disabled" not in a
                and (a.get("src") or "").endswith("/arrow_right.gif")
                and not self.next_button
            ):
                self.next_button = name
        elif tag == "select" and name:
            self._select, self._options = name, []
        elif tag == "option" and self._select:
            self._options.append((a.get("value") or "", "selected" in a))

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "select" and self._select:
            chosen = [v for v, selected in self._options if selected] or [v for v, _ in self._options[:1]]
            if chosen:
                self.fields[self._select] = chosen[0]
            self._select = None


class CourseTableParser(HTMLParser):
    """Reads the rows of the course list table (one row per course)."""

    ROW_CLASSES = {"DataGridItem", "DataGridAlternatingItem"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr" and a.get("class") in self.ROW_CLASSES:
            self._row = {"cells": {}, "id": None}
        elif self._row is None:
            return
        elif tag == "td":
            self._cell = a.get("class") or ""
            self._row["cells"][self._cell] = ""
        elif tag == "a" and self._cell == "DataGridItemCourseTitle":
            m = re.search(r"CourseDetail\.aspx\?id=(\d+)", a.get("href") or "")
            if m:
                self._row["id"] = m.group(1)
        elif tag == "br" and self._cell:
            self._row["cells"][self._cell] += " "

    def handle_data(self, data):
        if self._row is not None and self._cell:
            self._row["cells"][self._cell] += data

    def handle_endtag(self, tag):
        if self._row is None:
            return
        if tag == "td":
            self._cell = None
        elif tag == "tr":
            self.rows.append(self._row)
            self._row = None


def parse_form(page):
    parser = FormParser()
    parser.feed(page)
    return parser


def parse_courses(page):
    parser = CourseTableParser()
    parser.feed(page)
    courses = []
    for row in parser.rows:
        cell = lambda name: re.sub(r"\s+", " ", row["cells"].get(name, "")).strip()
        course = {
            "id": row["id"],
            "number": cell("DataGridItemCourseNumber"),
            "title": cell("DataGridItemCourseTitle"),
            "district": cell("DataGridItemDistrict"),
            "begin": cell("DataGridItemCourseBegin"),
            "places": cell("DataGridItemPlaces"),
            "hint": cell("DataGridItemBooking"),
        }
        course["available"] = is_available(course)
        courses.append(course)
    return courses


def free_places(places):
    """'belegt' -> 0, '2' -> 2, '> 2' -> 3; None when the list shows nothing (e.g. already started)."""
    m = re.fullmatch(r"(>\s*)?(\d+)", places)
    if m:
        return int(m.group(2)) + (1 if m.group(1) else 0)
    if places.lower() == "belegt":
        return 0
    return None


def is_available(course):
    return bool(free_places(course["places"])) and "nicht buchbar" not in course["hint"].lower()


class Session:
    def __init__(self):
        cookies = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        self.opener = urllib.request.build_opener(cookies)
        self.opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "de-DE,de;q=0.9")]

    def request(self, url, fields=None):
        data = None
        if fields is not None:
            data = urllib.parse.urlencode(fields, encoding=SITE_ENCODING, errors="replace").encode("ascii")
        with self.opener.open(url, data=data, timeout=60) as resp:
            return resp.read().decode(SITE_ENCODING, errors="replace")


def fetch_courses(search_term):
    """Runs the search like a browser would and returns every course in the result list."""
    session = Session()
    form = parse_form(session.request(SEARCH_URL))
    if SEARCH_BOX not in form.fields:
        raise ScrapeError("The search page looks different than expected (search box not found).")
    fields = dict(form.fields)
    fields[SEARCH_BOX] = search_term
    fields[SEARCH_BUTTON[0]] = SEARCH_BUTTON[1]
    page = session.request(SEARCH_URL, fields)

    if NO_RESULTS in page:
        return []
    m = re.search(r"Anzahl Treffer:\s*(\d+)", page)
    if not m:
        raise ScrapeError("The result page looks different than expected (no result count).")
    total = int(m.group(1))

    courses = {}
    for _ in range(MAX_PAGES):
        for course in parse_courses(page):
            courses[course["id"] or course["number"]] = course
        form = parse_form(page)
        if not form.next_button:
            break
        time.sleep(PAGE_DELAY)
        fields = dict(form.fields)
        # The pager arrows are image buttons; browsers send the click position.
        fields[form.next_button + ".x"] = "8"
        fields[form.next_button + ".y"] = "8"
        page = session.request(LIST_URL, fields)

    if len(courses) != total:
        raise ScrapeError(f"The site reported {total} courses but {len(courses)} could be read.")
    return list(courses.values())


def fetch_with_retries(search_term, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            return fetch_courses(search_term)
        except (ScrapeError, OSError, http.client.HTTPException) as e:
            if attempt == attempts:
                raise ScrapeError(f"Couldn't read the VHS course list: {e}") from e
            wait = 20 * attempt
            print(f"Attempt {attempt} failed ({e}); retrying in {wait}s.", file=sys.stderr)
            time.sleep(wait)


# --- Telegram ---------------------------------------------------------------


def telegram(token, method, payload=None, timeout=30):
    """Calls a Telegram Bot API method and returns its result."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            raise TelegramError(f"{method}: HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise TelegramError(f"{method}: {e}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'unknown error')}")
    return body["result"]


def send_message(token, chat_id, text):
    telegram(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


# --- Change detection and messages -------------------------------------------


def find_news(previous, courses, first_run):
    """Courses that are bookable now but weren't at the previous check."""
    news = []
    for course in courses:
        if not course["available"]:
            continue
        before = previous.get(course["id"] or course["number"])
        if first_run:
            news.append(("available", course))
        elif before is None:
            news.append(("new", course))
        elif not before.get("available"):
            news.append(("opened", course))
    return news


def pretty_begin(begin):
    """'28.10.2026 10:00 Uhr' -> 'Wed 28.10.2026, 10:00'"""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s*(\d{1,2}:\d{2})?", begin)
    if not m:
        return begin
    day, month, year, clock = m.groups()
    try:
        weekday = datetime.date(int(year), int(month), int(day)).strftime("%a")
    except ValueError:
        return begin
    return f"{weekday} {day}.{month}.{year}" + (f", {clock}" if clock else "")


def places_text(places):
    """'> 2' -> 'more than 2'"""
    return re.sub(r"^>\s*(\d+)$", r"more than \1", places)


def format_course(kind, course):
    esc = lambda s: html.escape(s, quote=False)
    heading = {
        "opened": "🟢 A place opened up",
        "new": "🆕 New date with free places",
        "available": "🟢 Free places right now",
    }[kind]
    link = DETAIL_URL.format(course["id"]) if course["id"] else SEARCH_URL
    lines = [
        f"<b>{heading}</b>",
        esc(course["title"]),
        f"🗓 {esc(pretty_begin(course['begin']))}",
        f"📍 {esc(course['district'])} · {esc(course['number'])}",
        f"🎟 Free places: {esc(places_text(course['places']))}",
    ]
    if course["hint"]:
        lines.append(f"ℹ️ {esc(course['hint'])}")
    lines.append(f'<a href="{link}">Open the course to book</a>')
    return "\n".join(lines)


def build_messages(news):
    """Joins the course blocks into as few Telegram messages as fit the length limit."""
    messages, current = [], ""
    for kind, course in news:
        block = format_course(kind, course)
        if current and len(current) + 2 + len(block) > MAX_MESSAGE_LEN:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


# --- State -------------------------------------------------------------------


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError:
        print(f"Warning: {path} is unreadable; starting fresh.", file=sys.stderr)
        return None


def save_state(path, courses):
    state = {"courses": {c["id"] or c["number"]: c for c in sorted(courses, key=lambda c: c["id"] or c["number"])}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# --- Main --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the messages instead of sending them; don't save state")
    args = parser.parse_args()

    search_term = os.environ.get("SEARCH_TERM") or DEFAULT_SEARCH_TERM
    title_filter = re.compile(os.environ.get("TITLE_FILTER") or DEFAULT_TITLE_FILTER, re.IGNORECASE)
    state_path = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not args.dry_run and not (token and chat_ids):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run).")

    try:
        courses = [c for c in fetch_with_retries(search_term) if title_filter.search(c["title"])]
    except ScrapeError as e:
        sys.exit(str(e))

    state = load_state(state_path)
    previous = (state or {}).get("courses", {})
    news = find_news(previous, courses, first_run=state is None)

    print(f"Search {search_term!r}: {len(courses)} matching courses, "
          f"{sum(c['available'] for c in courses)} with free places, {len(news)} to notify.")
    for c in courses:
        print(f"  {pretty_begin(c['begin']):<22} {c['district']:<28} {c['places'] or '-':<7} {c['title']}")
        if c["places"] and free_places(c["places"]) is None:
            print(f"    note: unrecognised free-places value {c['places']!r}")

    for text in build_messages(news):
        if args.dry_run:
            print("\n--- would send ---\n" + text)
            continue
        for chat_id in chat_ids:
            try:
                send_message(token, chat_id, text)
            except TelegramError as e:
                # Exit without saving, so the next run tries again.
                sys.exit(f"Couldn't send the Telegram message: {e}")

    if not args.dry_run:
        save_state(state_path, courses)


if __name__ == "__main__":
    main()

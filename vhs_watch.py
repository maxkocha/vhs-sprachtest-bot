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


class BookingError(Exception):
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
        self.url = None  # address of the last page loaded, after redirects

    def request(self, url, fields=None):
        data = None
        if fields is not None:
            data = urllib.parse.urlencode(fields, encoding=SITE_ENCODING, errors="replace").encode("ascii")
        with self.opener.open(url, data=data, timeout=60) as resp:
            self.url = resp.geturl()
            return resp.read().decode(SITE_ENCODING, errors="replace")

    def submit(self, page, extra):
        """Submits the page's ASP.NET form like a click would; extra holds the clicked
        button (or postback target) and any filled-in fields."""
        m = re.search(r'<form\b[^>]*\bid="aspnetForm"[^>]*>', page)
        action = re.search(r'\baction="([^"]*)"', m.group(0)) if m else None
        if not action:
            raise BookingError("The page has no form to submit.")
        fields = dict(parse_form(page).fields)
        fields.update(extra)
        time.sleep(PAGE_DELAY)
        return self.request(urllib.parse.urljoin(self.url, html.unescape(action.group(1))), fields)


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


# --- Booking with a VHS account --------------------------------------------------

ADD_TO_BASKET = ("ctl00$Content$btnAddToShoppingCard", "In den Warenkorb")  # on the course page
BASKET_LINK = "ctl00$LeftMenuContent$LeftMenu1$lbtnCart"
CHECKOUT = ("ctl00$Content$btnBookingSummary", "Zur Kasse gehen")
EMPTY_BASKET = ("ctl00$Content$btnClearShoppingCart", "Warenkorb leeren")
LOGIN = "ctl00$Content$CourseBookingChoiceContentControl1$"
CONFIRM_DELETE = ("ctl00$Content$btnOk", "Ja")  # in the "Wollen Sie jetzt den gesamten Warenkorb leeren?" dialog


def basket_count(page):
    """Number of courses in the basket, from the side menu's "Warenkorb (N)"."""
    m = re.search(r"Warenkorb\s*\((\d+)\)", re.sub(r"<[^>]+>", "", page))
    if not m:
        raise BookingError("The page doesn't show the basket count.")
    return int(m.group(1))


def add_to_basket(session, course_id):
    page = session.request(DETAIL_URL.format(course_id))
    if ADD_TO_BASKET[0] not in page:
        raise BookingError("The course page has no 'In den Warenkorb' button.")
    return session.submit(page, {ADD_TO_BASKET[0]: ADD_TO_BASKET[1]})


def open_basket(session, page):
    # Never with an empty basket: the site answers that with an error page.
    page = session.submit(page, {"__EVENTTARGET": BASKET_LINK, "__EVENTARGUMENT": ""})
    if CHECKOUT[0] not in page:
        raise BookingError("The basket page looks different than expected.")
    return page


def put_in_basket(session, course):
    """Adds the course to the basket from its course page; returns the basket page,
    which must hold exactly this one course."""
    page = add_to_basket(session, course["id"])
    if basket_count(page) != 1:
        raise BookingError(f"Expected just this date in the basket, found {basket_count(page)} courses.")
    page = open_basket(session, page)
    if course["number"] not in page:
        raise BookingError("The date didn't end up in the basket.")
    return page


def checkout_and_login(session, page, email, password):
    """From the basket: 'Zur Kasse gehen', then 'Ich möchte mich einloggen und buchen'."""
    page = session.submit(page, {CHECKOUT[0]: CHECKOUT[1]})
    if LOGIN + "txtPassword" not in page:
        raise BookingError("The checkout page has no login form.")
    page = session.submit(page, {
        LOGIN + "selection": "rdbLoginAndBook",
        LOGIN + "txtEmailAddress": email,
        LOGIN + "txtPassword": password,
        LOGIN + "btnContinue": "Weiter",
    })
    if LOGIN + "txtPassword" in page:
        raise BookingError("The VHS login didn't work: " + (page_messages(page) or "check the e-mail address and password."))
    return page


BOOK = ("ctl00$Content$ctl00$btnOnlineBooking", "Kostenpflichtig buchen")  # on the summary page


def content_of(page):
    """The page's main area, without the menus around it."""
    start, end = page.find("<!-- Content beginning -->"), page.find("<!-- Content end -->")
    return page[max(start, 0): end if end > start else len(page)]


def open_summary(session, course, email, password):
    """Basket, checkout, login: returns the summary page, which must list just this date."""
    page = checkout_and_login(session, put_in_basket(session, course), email, password)
    if BOOK[0] not in page:
        raise BookingError("The summary page has no 'Kostenpflichtig buchen' button.")
    if course["number"] not in content_of(page) or basket_count(page) != 1:
        raise BookingError("The summary doesn't list exactly this one date, so nothing was booked.")
    return page


def book(course, email, password):
    """Books the course with the VHS account. Raises BookingError if it stops before
    'Kostenpflichtig buchen'; once that is clicked, returns 'booked', 'waitlist' or 'unclear'."""
    session = Session()
    page = open_summary(session, course, email, password)
    try:
        result = session.submit(page, {BOOK[0]: BOOK[1]})
        count = basket_count(result)
    except (OSError, http.client.HTTPException, BookingError):
        return "unclear"
    if BOOK[0] in result:
        if count == 0:
            return "unclear"
        raise BookingError("VHS didn't take the booking: " + (page_messages(result) or "no reason given."))
    content = content_of(result)
    if "Warteliste" in content:
        return "waitlist"
    if count == 0 and re.search(r"Buchungsbestätigung|erfolgreich|Vielen Dank|gebucht", content, re.IGNORECASE):
        return "booked"
    return "unclear"


def try_booking(course, email, password):
    """Returns (status, reason): status is 'booked', 'waitlist', 'unclear' or 'failed'."""
    try:
        return book(course, email, password), None
    except BookingError as e:
        return "failed", str(e)
    except (OSError, http.client.HTTPException) as e:
        return "failed", f"the VHS site didn't respond properly ({e})"


def booking_check(courses, email, password):
    """Basket, checkout and login with a full date, stopping before 'Kostenpflichtig
    buchen'. Books nothing; raises BookingError if a step doesn't work."""
    full = [c for c in courses if c["id"] and not c["available"] and "warteliste" in c["hint"].lower()]
    if not full:
        raise BookingError("There's no full date to practise with right now.")
    session = Session()
    try:
        open_summary(session, full[0], email, password)
    finally:
        try:
            emptied = empty_basket(session, session.request(SEARCH_URL))
        except (BookingError, OSError):
            emptied = False
        if not emptied:
            print("Warning: couldn't empty the basket after the check (nothing was booked).")


def empty_basket(session, page):
    """Removes everything from the basket; returns True if it is empty afterwards."""
    if basket_count(page) == 0:
        return True
    page = session.submit(open_basket(session, page), {EMPTY_BASKET[0]: EMPTY_BASKET[1]})
    try:
        session.submit(page, dict([CONFIRM_DELETE]))
    except urllib.error.HTTPError as e:
        # The site empties the basket, then fails to show the empty basket (HTTP 500).
        if e.code != 500:
            raise
    return basket_count(session.request(SEARCH_URL)) == 0


class _PageOutline(HTMLParser):
    """Collects a page's headings, buttons, check boxes and messages, but no text-field values."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls, self.labels, self.headings, self.messages = [], {}, [], []
        self._label = self._capture = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "input" and (a.get("type") or "text").lower() != "hidden" and a.get("name"):
            kind = (a.get("type") or "text").lower()
            shown = a.get("value") if kind in ("submit", "button", "radio", "checkbox") else None
            self.controls.append((kind, a["name"], shown, "checked" in a, "disabled" in a, a.get("id")))
        elif tag in ("select", "textarea") and a.get("name"):
            self.controls.append((tag, a["name"], None, False, "disabled" in a, a.get("id")))
        elif tag == "label":
            self._label = [a.get("for"), ""]
        elif tag in ("h1", "h2", "h3"):
            self._capture = [self.headings, ""]
        elif tag == "span" and re.search(r"Error|Message|Hinweis|Info", a.get("id") or ""):
            self._capture = [self.messages, ""]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag == "label" and self._label:
            self.labels[self._label[0]] = " ".join(self._label[1].split())
            self._label = None
        elif tag in ("h1", "h2", "h3", "span") and self._capture:
            text = " ".join(self._capture[1].split())
            if text:
                self._capture[0].append(text)
            self._capture = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._label:
            self._label[1] += data
        if self._capture:
            self._capture[1] += data


def page_messages(page):
    outline = _PageOutline()
    outline.feed(page)
    return " | ".join(m for m in outline.messages if len(m) < 300)


def describe_page(page):
    """A privacy-safe outline of a page's main area: headings, buttons and check boxes
    with their labels, messages, and sentences about the terms. No form values, and
    e-mail addresses and IBANs are masked."""
    content = content_of(page)
    outline = _PageOutline()
    outline.feed(content)
    body = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", content)
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))
    keywords = r"AGB|Geschäftsbeding|Widerruf|Datenschutz|verbindlich|kostenpflichtig|Warteliste|Lastschrift|Mandat|akzeptier|Kenntnis|bestätig"
    sentences = [s.strip()[:220] for s in re.split(r"(?<=[.!?:])\s+", text) if re.search(keywords, s)]
    lines = ["headings: " + " | ".join(outline.headings[:6])]
    for kind, name, value, checked, disabled, cid in outline.controls:
        if "LeftMenu" in name or name in ("q",):
            continue
        label = outline.labels.get(cid, "")
        lines.append(f"  {kind:9} {name}"
                     + (f" = {value!r}" if value is not None else "")
                     + (f"  label={label!r}" if label else "")
                     + (" [checked]" if checked else "") + (" [disabled]" if disabled else ""))
    if outline.messages:
        lines.append("messages: " + " | ".join(m for m in outline.messages if len(m) < 300))
    lines += ["text: " + s for s in dict.fromkeys(sentences)][:12]
    outline_text = "\n".join(lines)
    outline_text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<e-mail>", outline_text)
    return re.sub(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b", "<IBAN>", outline_text)


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


def begin_key(course):
    """Sort key for the start: '28.10.2026 10:00 Uhr' -> ('2026', '10', '28', '10:00')."""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s*(\d{1,2}:\d{2})?", course["begin"])
    if not m:
        return ("9999",)
    day, month, year, clock = m.groups()
    return (year, month, day, (clock or "").zfill(5))


def format_booking(status, course, reason=None):
    esc = lambda s: html.escape(s, quote=False)
    heading, notes = {
        "booked": ("✅ <b>Booked for you</b>", [
            "VHS will e-mail you the booking confirmation; the fee is taken by direct debit.",
            "Cancelling is free by e-mail within 14 days of booking, but not from 13 days before the exam.",
            "Automatic booking is now off, so I won't book a second date."]),
        "waitlist": ("📝 <b>The place was gone, so you're on the waitlist</b>", [
            "VHS will get in touch if a place frees up. Automatic booking is now off."]),
        "unclear": ("⚠️ <b>I clicked 'Kostenpflichtig buchen' but couldn't confirm the booking</b>", [
            "Check your e-mail for a VHS confirmation, or log in to your VHS account.",
            "Automatic booking is now off, so it can't book twice."]),
        "failed": ("⚠️ <b>Couldn't book automatically</b>", [
            f"Reason: {esc(reason or 'unknown')}",
            "Book it yourself now with the link below."]),
    }[status]
    return "\n".join([
        heading,
        esc(course["title"]),
        f"🗓 {esc(pretty_begin(course['begin']))}",
        f"📍 {esc(course['district'])} · {esc(course['number'])}",
        *notes,
        f'<a href="{DETAIL_URL.format(course["id"])}">Open the course</a>',
    ])


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


def save_state(path, courses, auto_booking_done=False):
    state = {"courses": {c["id"] or c["number"]: c for c in sorted(courses, key=lambda c: c["id"] or c["number"])}}
    if auto_booking_done:
        state["auto_booking_done"] = True
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# --- Main --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the messages instead of sending them; don't book or save state")
    parser.add_argument("--booking-check", action="store_true",
                        help="only test the VHS login and booking steps with a full date; books nothing")
    args = parser.parse_args()

    search_term = os.environ.get("SEARCH_TERM") or DEFAULT_SEARCH_TERM
    title_filter = re.compile(os.environ.get("TITLE_FILTER") or DEFAULT_TITLE_FILTER, re.IGNORECASE)
    state_path = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    # With a VHS login, the earliest newly bookable date is booked automatically (once).
    vhs_email = os.environ.get("VHS_EMAIL", "").strip()
    vhs_password = os.environ.get("VHS_PASSWORD", "")
    if not args.dry_run and not args.booking_check and not (token and chat_ids):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run).")

    try:
        courses = [c for c in fetch_with_retries(search_term) if title_filter.search(c["title"])]
    except ScrapeError as e:
        sys.exit(str(e))

    if args.booking_check:
        if not (vhs_email and vhs_password):
            sys.exit("Set VHS_EMAIL and VHS_PASSWORD for the booking check.")
        try:
            booking_check(courses, vhs_email, vhs_password)
        except (BookingError, OSError, http.client.HTTPException) as e:
            sys.exit(f"Booking check failed: {e}")
        print("Booking check passed: logged in and reached 'Kostenpflichtig buchen'. Nothing was booked.")
        return

    state = load_state(state_path)
    previous = (state or {}).get("courses", {})
    news = find_news(previous, courses, first_run=state is None)
    auto_booking_done = bool((state or {}).get("auto_booking_done"))

    print(f"Search {search_term!r}: {len(courses)} matching courses, "
          f"{sum(c['available'] for c in courses)} with free places, {len(news)} to notify.")
    for c in courses:
        print(f"  {pretty_begin(c['begin']):<22} {c['district']:<28} {c['places'] or '-':<7} {c['title']}")
        if c["places"] and free_places(c["places"]) is None:
            print(f"    note: unrecognised free-places value {c['places']!r}")

    booking_texts = []
    if news and vhs_email and vhs_password and not auto_booking_done:
        target = min((c for _, c in news), key=begin_key)
        if args.dry_run:
            print(f"Would try to book {target['number']} automatically.")
        else:
            status, reason = try_booking(target, vhs_email, vhs_password)
            print(f"Automatic booking: {status}" + (f" ({reason})" if reason else ""))
            booking_texts.append(format_booking(status, target, reason))
            if status != "failed":
                # Remember this before sending anything, so no later run can book a second date.
                auto_booking_done = True
                save_state(state_path, courses, auto_booking_done=True)
                news = [(kind, c) for kind, c in news if c is not target]

    for text in booking_texts + build_messages(news):
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
        save_state(state_path, courses, auto_booking_done)


if __name__ == "__main__":
    main()

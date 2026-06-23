#!/usr/bin/env python3
"""
Scrape the CurlVegas calendar and write events to docs/calendar.ics.

The calendar is a Joomla `com_facilitycalendar` component backed by
FullCalendar. FullCalendar loads events via a POST to:

    index.php?option=com_facilitycalendar&task=calendar.getevents

with the visible date range (start/end) plus calview/types params. The
response is a JSON array of FullCalendar event objects (despite being served
with a text/html content-type). The endpoint only returns one month of events
at a time, so we POST it once per month across the window below and merge the
results — far more reliable and faster than driving a headless browser.

The merged raw JSON is dumped to docs/_debug.json for troubleshooting.
"""

import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from icalendar import Calendar, Event
from pytz import timezone as pytz_timezone

BASE_URL = "https://curlvegas.com/index.php"
EVENTS_TASK = "com_facilitycalendar&task=calendar.getevents"
EVENTS_URL = f"{BASE_URL}?option={EVENTS_TASK}"
OUT_PATH = Path("docs/calendar.ics")
DEBUG_JSON = Path("docs/_debug.json")
DEBUG_ENDPOINTS = Path("docs/_endpoints.txt")
LOCAL_TZ = pytz_timezone("America/Los_Angeles")  # CurlVegas is in Las Vegas

# How wide a window of months to fetch, relative to the current month.
MONTHS_BACK = 2
MONTHS_AHEAD = 12

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


MAX_TRIES = 5
# Delays between attempts: 5, 15, 45, 120 s — total up to ~3 minutes.
_BACKOFF_BASE = 5
_BACKOFF_FACTOR = 3
_BACKOFF_MAX = 120


def fetch_events(session, start, end):
    """POST one [start, end) window to the events endpoint and return its events."""
    for attempt in range(MAX_TRIES):
        try:
            resp = session.post(
                EVENTS_URL,
                data={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "calview": "dayGridMonth",
                    "types": "",
                },
                headers=HEADERS,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"Expected a JSON list of events, got {type(data).__name__}")
            return data
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_TRIES - 1:
                raise
            delay = min(_BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt), _BACKOFF_MAX)
            print(
                f"  {start:%Y-%m} attempt {attempt + 1}/{MAX_TRIES} failed "
                f"({exc}); retrying in {delay}s…",
                file=sys.stderr,
            )
            time.sleep(delay)


def parse_dt(value):
    """Parse a date/datetime string; assume Las Vegas local time if naive."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    try:
        dt = date_parser.parse(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return LOCAL_TZ.localize(dt)
    # Normalize fixed-offset datetimes into the named local zone so icalendar
    # emits a proper TZID rather than ambiguous floating time.
    return dt.astimezone(LOCAL_TZ)


def stable_uid(raw):
    """Derive a stable UID from event content (events carry no id of their own)."""
    props = raw.get("extendedProps") or {}
    if props.get("ext_id") and props["ext_id"] not in ("0", 0):
        return f"{props.get('ext', 'ext')}-{props['ext_id']}"
    blob = json.dumps(
        {
            "t": raw.get("title"),
            "s": raw.get("start"),
            "e": raw.get("end"),
        },
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def build_calendar(events_by_uid):
    cal = Calendar()
    cal.add("prodid", "-//curlvegas-scraper//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "CurlVegas Calendar")
    cal.add("x-wr-timezone", "America/Los_Angeles")

    written = 0
    for uid, raw in events_by_uid.items():
        start = parse_dt(raw.get("start"))
        if start is None:
            continue
        end = parse_dt(raw.get("end"))

        props = raw.get("extendedProps") or {}

        # The feed returns curling league games (ext=com_curling) with an empty
        # title — the site fetches the matchup name from a separate component
        # that has no public endpoint, so we fall back to a generic label.
        title = (raw.get("title") or "").strip()
        if not title:
            title = "Curling Game" if props.get("ext") == "com_curling" else "Untitled"

        ev = Event()
        ev.add("summary", title)
        ev.add("dtstart", start)
        if end is not None:
            ev.add("dtend", end)

        desc_bits = []
        for label, key in (("Description", "description"), ("Comment", "comment")):
            v = props.get(key)
            if v and str(v).strip():
                desc_bits.append(f"{label}: {str(v).strip()}")
        if desc_bits:
            ev.add("description", "\n".join(desc_bits))

        ev.add("uid", f"{uid}@curlvegas.scrape")
        cal.add_component(ev)
        written += 1

    return cal, written


def main():
    # First day of the current month, then step one month at a time across the
    # window. The endpoint only returns a single month per request.
    first_of_month = date.today().replace(day=1)
    window_start = first_of_month - relativedelta(months=MONTHS_BACK)

    all_raw = []
    # De-dupe across months (events recur across windows, and the endpoint can
    # repeat multi-resource events).
    events_by_uid = {}

    with requests.Session() as session:
        for i in range(MONTHS_BACK + MONTHS_AHEAD + 1):
            start = window_start + relativedelta(months=i)
            end = start + relativedelta(months=1)
            batch = fetch_events(session, start, end)
            print(f"{start:%Y-%m}: {len(batch)} events")
            for raw in batch:
                if isinstance(raw, dict):
                    all_raw.append(raw)
                    events_by_uid[stable_uid(raw)] = raw

    DEBUG_JSON.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_JSON.write_text(json.dumps(all_raw, indent=2), encoding="utf-8")
    DEBUG_ENDPOINTS.write_text(EVENTS_URL + "\n", encoding="utf-8")

    cal, written = build_calendar(events_by_uid)
    OUT_PATH.write_bytes(cal.to_ical())
    print(
        f"Wrote {written} unique events to {OUT_PATH} "
        f"({len(all_raw)} fetched across {MONTHS_BACK + MONTHS_AHEAD + 1} months)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

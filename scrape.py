#!/usr/bin/env python3
"""
Scrape the CurlVegas calendar and write events to docs/calendar.ics.

The calendar is a Joomla `com_facilitycalendar` component backed by
FullCalendar. FullCalendar loads events via a POST to:

    index.php?option=com_facilitycalendar&task=calendar.getevents

with the visible date range (start/end) plus calview/types params. The
response is a JSON array of FullCalendar event objects (despite being served
with a text/html content-type). We POST to that endpoint directly for a wide
date window — far more reliable and faster than driving a headless browser.

The raw JSON response is dumped to docs/_debug.json for troubleshooting.
"""

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dateutil import parser as date_parser
from icalendar import Calendar, Event
from pytz import timezone as pytz_timezone

BASE_URL = "https://curlvegas.com/index.php"
EVENTS_TASK = "com_facilitycalendar&task=calendar.getevents"
OUT_PATH = Path("docs/calendar.ics")
DEBUG_JSON = Path("docs/_debug.json")
DEBUG_ENDPOINTS = Path("docs/_endpoints.txt")
LOCAL_TZ = pytz_timezone("America/Los_Angeles")  # CurlVegas is in Las Vegas

# How wide a window of events to fetch, relative to today.
DAYS_BACK = 31
DAYS_AHEAD = 365


def fetch_events(start, end):
    """POST to the FullCalendar events endpoint and return a list of events."""
    url = f"{BASE_URL}?option={EVENTS_TASK}"
    resp = requests.post(
        url,
        data={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calview": "dayGridMonth",
            "types": "",
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        timeout=60,
    )
    resp.raise_for_status()
    DEBUG_JSON.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_JSON.write_text(resp.text, encoding="utf-8")
    DEBUG_ENDPOINTS.write_text(url + "\n", encoding="utf-8")

    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list of events, got {type(data).__name__}")
    return data


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
    today = date.today()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_AHEAD)

    print(f"Fetching events from {start} to {end} ...")
    batch = fetch_events(start, end)
    print(f"Endpoint returned {len(batch)} events.")

    # De-dupe (the endpoint can repeat multi-resource events across resources)
    events_by_uid = {}
    for raw in batch:
        if isinstance(raw, dict):
            events_by_uid[stable_uid(raw)] = raw

    cal, written = build_calendar(events_by_uid)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(cal.to_ical())
    print(f"Wrote {written} unique events to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

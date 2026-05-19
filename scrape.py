#!/usr/bin/env python3
"""
Scrape the CurlVegas calendar and write events to docs/calendar.ics.

Strategy: load the page in a headless browser, intercept network responses,
keep any JSON response that looks like a list of calendar events. This is
much more reliable than parsing the rendered DOM because we get the same
structured data the website itself uses.

If no event JSON is intercepted, we still write a valid (empty) ICS file
and dump the page HTML to docs/_debug.html so you can troubleshoot.
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from dateutil import parser as date_parser
from icalendar import Calendar, Event
from playwright.async_api import async_playwright
from pytz import timezone as pytz_timezone

CALENDAR_URL = "https://curlvegas.com/index.php/calendar"
OUT_PATH = Path("docs/calendar.ics")
DEBUG_HTML = Path("docs/_debug.html")
DEBUG_ENDPOINTS = Path("docs/_endpoints.txt")
LOCAL_TZ = pytz_timezone("America/Los_Angeles")  # CurlVegas is in Las Vegas


def looks_like_events(data):
    """Return True if data looks like a list of FullCalendar-style event objects."""
    if not isinstance(data, list) or not data:
        return False
    sample = data[0]
    if not isinstance(sample, dict):
        return False
    keys = {k.lower() for k in sample.keys()}
    has_title = "title" in keys
    has_start = "start" in keys or "start_date" in keys or "startdate" in keys
    return has_title and has_start


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
        dt = LOCAL_TZ.localize(dt)
    return dt


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
        start = parse_dt(raw.get("start") or raw.get("start_date") or raw.get("startDate"))
        if start is None:
            continue
        end = parse_dt(raw.get("end") or raw.get("end_date") or raw.get("endDate"))

        ev = Event()
        ev.add("summary", str(raw.get("title") or "Untitled").strip())
        ev.add("dtstart", start)
        if end is not None:
            ev.add("dtend", end)
        location = (
            raw.get("location")
            or raw.get("resource")
            or raw.get("resourceTitle")
            or raw.get("resourceId")
        )
        if location:
            ev.add("location", str(location))

        desc_bits = []
        for k in ("description", "notes", "comments", "booking_use", "bookingUse"):
            v = raw.get(k)
            if v:
                desc_bits.append(f"{k}: {v}")
        if desc_bits:
            ev.add("description", "\n".join(desc_bits))

        ev.add("uid", f"{uid}@curlvegas.scrape")
        cal.add_component(ev)
        written += 1

    return cal, written


def stable_uid(raw):
    """Derive a stable UID from event content when no id is provided."""
    if raw.get("id"):
        return str(raw["id"])
    blob = json.dumps(
        {
            "t": raw.get("title"),
            "s": raw.get("start") or raw.get("start_date"),
            "e": raw.get("end") or raw.get("end_date"),
            "r": raw.get("resource") or raw.get("resourceId"),
        },
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


async def main():
    captured_batches = []
    captured_endpoints = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                ct = (response.headers.get("content-type") or "").lower()
                if "json" not in ct and not response.url.lower().endswith(".json"):
                    return
                body = await response.text()
                if "title" not in body.lower() or "start" not in body.lower():
                    return
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return
                # Some endpoints wrap the list in a dict
                if isinstance(data, dict):
                    for key in ("events", "data", "items", "results"):
                        if key in data and looks_like_events(data[key]):
                            captured_batches.append(data[key])
                            captured_endpoints.append(response.url)
                            return
                if looks_like_events(data):
                    captured_batches.append(data)
                    captured_endpoints.append(response.url)
            except Exception as e:
                print(f"  (response handler error: {e})", file=sys.stderr)

        page.on("response", on_response)

        print(f"Loading {CALENDAR_URL} ...")
        await page.goto(CALENDAR_URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(5000)

        # If nothing came in, try nudging FullCalendar to fetch by clicking next/prev
        if not captured_batches:
            print("No events captured on initial load; trying navigation buttons...")
            for selector in [
                "button.fc-next-button",
                "button.fc-prev-button",
                "button.fc-today-button",
                "button.fc-dayGridMonth-button",
                "button.fc-listMonth-button",
            ]:
                try:
                    locator = page.locator(selector)
                    if await locator.count():
                        await locator.first.click(timeout=2000)
                        await page.wait_for_timeout(2500)
                        if captured_batches:
                            break
                except Exception:
                    pass

        # Save debug snapshot regardless — small and very helpful if scraping breaks
        DEBUG_HTML.parent.mkdir(parents=True, exist_ok=True)
        try:
            html = await page.content()
            DEBUG_HTML.write_text(html, encoding="utf-8")
        except Exception:
            pass

        await browser.close()

    # De-dupe and merge across all captured batches
    events_by_uid = {}
    for batch in captured_batches:
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            events_by_uid[stable_uid(raw)] = raw

    print(
        f"Captured {len(events_by_uid)} unique events from "
        f"{len(set(captured_endpoints))} endpoint(s)."
    )
    for ep in sorted(set(captured_endpoints)):
        print(f"  - {ep}")

    DEBUG_ENDPOINTS.write_text("\n".join(sorted(set(captured_endpoints))) + "\n")

    cal, written = build_calendar(events_by_uid)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(cal.to_ical())
    print(f"Wrote {written} events to {OUT_PATH}")

    # Exit cleanly even if zero events — empty calendar is still a valid feed
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

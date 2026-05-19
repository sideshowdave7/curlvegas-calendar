# curlvegas-calendar

Scrapes the [CurlVegas](https://curlvegas.com/index.php/calendar) calendar every 6
hours via GitHub Actions and publishes the result as an iCalendar feed at
`docs/calendar.ics`, served from GitHub Pages.

## Subscribe

Once Pages is enabled (Settings → Pages → Source: **GitHub Actions**), the feed
URL is:

```
https://<your-username>.github.io/curlvegas-calendar/calendar.ics
```

Add that URL to Google Calendar, Apple Calendar, Outlook, etc. as a subscribed
calendar.

## How it works

`scrape.py` loads the calendar page in headless Chromium (Playwright), watches
the network for JSON responses that look like FullCalendar events, de-duplicates
them, and writes a valid ICS file. If nothing is captured it still writes an
empty (but valid) feed plus `docs/_debug.html` for troubleshooting.

The `.github/workflows/scrape.yml` workflow runs on a 6-hour cron (and on
manual dispatch), then deploys `docs/` to GitHub Pages.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python scrape.py
```

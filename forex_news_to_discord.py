#!/usr/bin/env python3
"""
forex_news_to_discord.py

Posts a morning brief to a Discord channel via webhook:
  1. "Red folder" (High-impact) economic events from the Forex Factory calendar feed.
  2. Recent market-relevant Trump headlines from Google News.

No third-party dependencies -- standard library only (works anywhere with Python 3.9+).

Required environment variable:
  DISCORD_WEBHOOK_URL    Your Discord channel webhook URL.

Optional environment variables (defaults in brackets):
  TIMEZONE               IANA tz name for "today" + display.        [Europe/London]
  CURRENCIES             Comma list to restrict events, e.g. USD,EUR.  [empty = all]
  IMPACTS                Comma list of impacts to include.          [High]   (High = red folder)
  TRUMP_QUERY            Google News search query.                  [market-relevant default]
  TRUMP_MAX              Max headlines to show.                     [8]
  TRUMP_LOOKBACK_HOURS   Only headlines newer than this many hrs.   [24]
  POST_IF_EMPTY          "1" to still post when nothing is found.   [1]
  EXPECTED_LOCAL_HOUR    If set, exit unless the local hour matches (DST guard for cron). [unset]
  STATE_FILE             File that stores the last post's id (so it can be deleted). [last_message_id.txt]
"""

import os
import sys
import json
import html
import urllib.parse
import urllib.request
import urllib.error
import datetime as dt
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ is required (needs the zoneinfo module).")

# --------------------------------------------------------------------------- config
FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

WEBHOOK    = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
TZ         = ZoneInfo(os.environ.get("TIMEZONE", "Europe/London"))
IMPACTS    = {s.strip() for s in os.environ.get("IMPACTS", "High").split(",") if s.strip()}
CURRENCIES = {s.strip().upper() for s in os.environ.get("CURRENCIES", "").split(",") if s.strip()}
TRUMP_MAX  = int(os.environ.get("TRUMP_MAX", "8"))
LOOKBACK   = int(os.environ.get("TRUMP_LOOKBACK_HOURS", "24"))
POST_IF_EMPTY = os.environ.get("POST_IF_EMPTY", "1") == "1"
EXPECTED_HOUR = os.environ.get("EXPECTED_LOCAL_HOUR", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "last_message_id.txt")  # remembers the post to delete next run

DEFAULT_TRUMP_QUERY = (
    'Trump (tariff OR tariffs OR "Federal Reserve" OR Fed OR Powell OR '
    'dollar OR trade OR sanctions OR economy OR "interest rates")'
)
TRUMP_QUERY = os.environ.get("TRUMP_QUERY", DEFAULT_TRUMP_QUERY)

UA = "Mozilla/5.0 (compatible; forex-news-bot/1.0)"
IMPACT_EMOJI = {"High": "\U0001F534", "Medium": "\U0001F7E0",
                "Low": "\U0001F7E1", "Holiday": "\u26AA", "Non-Economic": "\u26AA"}


# --------------------------------------------------------------------------- helpers
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def get_economic_events():
    """Return today's matching events as tuples sorted by time."""
    text = http_get(FF_FEED).decode("utf-8", "replace")
    if "Request Denied" in text or text.lstrip()[:9].lower() == "<!doctype":
        raise RuntimeError("Forex Factory feed is rate-limited ('Request Denied'); "
                           "fetch at most ~once per hour.")
    events = json.loads(text)
    today = dt.datetime.now(TZ).date()
    chosen = []
    for e in events:
        impact = (e.get("impact") or "").strip()
        if IMPACTS and impact not in IMPACTS:
            continue
        cur = (e.get("country") or "").strip().upper()   # 'country' field holds the currency code
        if CURRENCIES and cur not in CURRENCIES:
            continue
        raw_date = e.get("date")
        if not raw_date:
            continue
        try:
            when = dt.datetime.fromisoformat(raw_date).astimezone(TZ)
        except ValueError:
            continue
        if when.date() != today:
            continue
        chosen.append((
            when, cur, impact,
            (e.get("title") or "").strip(),
            (e.get("forecast") or "").strip(),
            (e.get("previous") or "").strip(),
        ))
    chosen.sort(key=lambda x: x[0])
    return chosen


def get_trump_headlines():
    """Return recent, de-duplicated headlines as (when, title, link) tuples."""
    q = urllib.parse.quote(TRUMP_QUERY)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    root = ET.fromstring(http_get(url))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK)
    items, seen = [], set()
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        when = None
        if pub:
            try:
                when = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                when = None
        if when and when < cutoff:
            continue
        key = title.split(" - ")[0].lower()   # Google appends " - Publisher"
        if not key or key in seen:
            continue
        seen.add(key)
        items.append((when, title, link))
    items.sort(key=lambda x: (x[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
               reverse=True)
    return items[:TRUMP_MAX]


# --------------------------------------------------------------------------- formatting
def format_events(events):
    if not events:
        return "_No red-folder events scheduled today._"
    lines = []
    for when, cur, impact, title, forecast, prev in events:
        emoji = IMPACT_EMOJI.get(impact, "\U0001F534")
        extra = []
        if forecast:
            extra.append(f"F: {forecast}")
        if prev:
            extra.append(f"P: {prev}")
        tail = f"  ({' | '.join(extra)})" if extra else ""
        lines.append(f"`{when:%H:%M}` {emoji} **{cur}** \u2014 {title}{tail}")
    return "\n".join(lines)


def format_headlines(items):
    if not items:
        return f"_No relevant Trump headlines in the last {LOOKBACK}h._"
    lines = []
    for when, title, link in items:
        ts = f"`{when.astimezone(TZ):%H:%M}` " if when else ""
        lines.append(f"{ts}\u2022 [{title}]({link})" if link else f"{ts}\u2022 {title}")
    return "\n".join(lines)


def clamp(s, limit=4096):
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


def _webhook_parts():
    base, _, query = WEBHOOK.partition("?")
    return base.rstrip("/"), query


def post(embeds):
    """Post a new webhook message and return its message id (?wait=true gives us the id)."""
    base, query = _webhook_parts()
    url = base + "?wait=true" + (("&" + query) if query else "")
    payload = {"username": "Morning Forex Brief", "embeds": embeds}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("id")
    except (ValueError, AttributeError):
        return None


def delete_message(message_id):
    """Delete a message we posted earlier. Tolerates 'already gone' (404)."""
    if not message_id:
        return
    base, query = _webhook_parts()
    url = f"{base}/messages/{message_id}" + (("?" + query) if query else "")
    req = urllib.request.Request(url, method="DELETE", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:        # 404 = manually deleted already; anything else is real
            raise


def read_last_id():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def write_last_id(message_id):
    if message_id:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(message_id))


# --------------------------------------------------------------------------- main
def main():
    if not WEBHOOK:
        sys.exit("ERROR: set the DISCORD_WEBHOOK_URL environment variable.")

    now = dt.datetime.now(TZ)
    if EXPECTED_HOUR and str(now.hour) != EXPECTED_HOUR:
        print(f"Local hour {now.hour} != EXPECTED_LOCAL_HOUR {EXPECTED_HOUR}; skipping.")
        return

    today_str = now.strftime("%A, %B ") + str(now.day) + now.strftime(", %Y")

    try:
        events = get_economic_events()
        ev_text = format_events(events)
    except Exception as e:               # network / rate-limit / parse issues
        events, ev_text = [], f"\u26A0\uFE0F Could not load events: {e}"

    try:
        heads = get_trump_headlines()
        hd_text = format_headlines(heads)
    except Exception as e:
        heads, hd_text = [], f"\u26A0\uFE0F Could not load headlines: {e}"

    if not POST_IF_EMPTY and not events and not heads \
            and "\u26A0\uFE0F" not in ev_text and "\u26A0\uFE0F" not in hd_text:
        print("Nothing to post and POST_IF_EMPTY=0; skipping.")
        return

    embeds = [
        {"title": f"\U0001F534 Red-Folder Economic Events \u2014 {today_str}",
         "description": clamp(ev_text), "color": 0xE03131},
        {"title": "\U0001F5DE\uFE0F Trump / Market Headlines",
         "description": clamp(hd_text), "color": 0x1971C2},
    ]
    prev_id = read_last_id()
    if prev_id:
        delete_message(prev_id)          # remove the previous post before posting the new one
        print(f"Deleted previous post {prev_id}.")

    new_id = post(embeds)
    write_last_id(new_id)
    print(f"Posted brief: {len(events)} event(s), {len(heads)} headline(s). id={new_id}")


if __name__ == "__main__":
    main()

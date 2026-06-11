#!/usr/bin/env python3
"""
forex_news_to_discord.py

Posts a pretty morning brief to a Discord channel via webhook:
  1. "Red folder" (High-impact) economic events from the Forex Factory calendar feed,
     each with a plain-English "what it is / why it matters" line.
  2. A "Trump Watch" line built from recent market-relevant Google News headlines.
  3. Pairs to watch, a rotating market quote, and a sign-off.

No third-party dependencies -- standard library only (Python 3.9+).

Required environment variable:
  DISCORD_WEBHOOK_URL    Your Discord channel webhook URL.

Optional environment variables (defaults in brackets):
  TIMEZONE               IANA tz name for "today" + display.        [Europe/London]
  CURRENCIES             Comma list to restrict events, e.g. USD,EUR.  [empty = all]
  IMPACTS                Comma list of impacts to include.          [High]  (High = red folder)
  TRUMP_QUERY            Google News search query.                  [market-relevant default]
  TRUMP_MAX              Max headlines to scan.                     [8]
  TRUMP_SHOW             Max Trump headlines to show.               [3]
  TRUMP_LOOKBACK_HOURS   Only headlines newer than this many hrs.   [24]
  GREETING               Opening line, posted as text so it can ping.  [Morning @everyone]
  BRIEF_TITLE            Header title.                              [Forex Morning Briefing]
  BRAND                  Name the closing quote is signed with.     [Team Inner Edge]
  SIGNOFF                Optional closing line.                     [none]
  WEBHOOK_USERNAME       Name the bot posts under.                  [TEAM INNER EDGE]
  AVATAR_URL             Public image URL to use as the bot logo.   [none = use webhook's avatar]
  EMBED_COLOR            Hex colour of the embed's left bar.        [0xE03131]
  PING_EVERYONE          "1" to let the greeting notify @everyone.  [1]
  POST_IF_EMPTY          "1" to still post when nothing is found.   [1]
  EXPECTED_LOCAL_HOUR    If set, exit unless local hour matches (DST guard for cron). [unset]
  STATE_FILE             Stores the ids of the posts to delete next run. [last_message_id.txt]
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
TRUMP_SHOW = int(os.environ.get("TRUMP_SHOW", "3"))
LOOKBACK   = int(os.environ.get("TRUMP_LOOKBACK_HOURS", "24"))
POST_IF_EMPTY = os.environ.get("POST_IF_EMPTY", "1") == "1"
EXPECTED_HOUR = os.environ.get("EXPECTED_LOCAL_HOUR", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "last_message_id.txt")

GREETING    = os.environ.get("GREETING", "Morning @everyone \U0001F44B")
BRIEF_TITLE = os.environ.get("BRIEF_TITLE", "Forex Morning Briefing")
BRAND       = os.environ.get("BRAND", "Team Inner Edge")
SIGNOFF     = os.environ.get("SIGNOFF", "")
WEBHOOK_USERNAME = os.environ.get("WEBHOOK_USERNAME", "TEAM INNER EDGE")
AVATAR_URL  = os.environ.get("AVATAR_URL", "").strip()      # optional logo override (public image URL)
EMBED_COLOR = int(os.environ.get("EMBED_COLOR", "0x5865F2"), 0)  # left-bar colour of the embed
PING_EVERYONE = os.environ.get("PING_EVERYONE", "1") == "1"  # let the greeting actually notify
RULE = "\u25AC" * 18

# Times are shown in the primary tz (TIMEZONE) and a second tz side by side.
TZ_LABEL    = os.environ.get("TZ_LABEL", "UK")
SECOND_TZ   = ZoneInfo(os.environ.get("SECOND_TZ", "America/New_York"))
SECOND_LABEL = os.environ.get("SECOND_TZ_LABEL", "EST")

DEFAULT_TRUMP_QUERY = (
    'Trump (tariff OR tariffs OR "Federal Reserve" OR Fed OR Powell OR '
    'dollar OR trade OR sanctions OR economy OR "interest rates")'
)
TRUMP_QUERY = os.environ.get("TRUMP_QUERY", DEFAULT_TRUMP_QUERY)

UA = "Mozilla/5.0 (compatible; forex-news-bot/1.0)"

# FX pair naming convention: the base currency is whichever appears earlier here.
PAIR_ORDER = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]

# Short, common trading aphorisms (rotated daily). Signed with BRAND.
QUOTES = [
    "The trend is your friend - until it ends.",
    "Plan the trade, and trade the plan.",
    "Cut your losses short and let your winners run.",
    "Risk comes from not knowing what you're doing.",
    "Patience is a position.",
    "The market can stay irrational longer than you can stay solvent.",
    "Trade what you see, not what you think.",
    "Discipline beats conviction.",
    "When in doubt, stay out.",
    "Protect your capital first; profits come second.",
]


# --------------------------------------------------------------------------- fetch
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
        cur = (e.get("country") or "").strip().upper()   # 'country' holds the currency code
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


# --------------------------------------------------------------------------- writing
def describe(title):
    """A plain-English 'what it is / why it matters' line for an event."""
    t = title.lower()
    if "core pce" in t or "pce" in t:
        return "The Fed's preferred inflation gauge."
    if "cpi" in t or "consumer price" in t or "inflation" in t:
        return "Measures inflation - a key input for central-bank policy."
    if "ppi" in t or "producer price" in t:
        return "Wholesale price pressure that often feeds through into inflation."
    if ("rate" in t and ("decision" in t or "statement" in t)) or "cash rate" in t \
            or "bank rate" in t or "interest rate" in t:
        return "The central bank's interest-rate decision - a major driver for the currency."
    if "non-farm" in t or "nonfarm" in t or "nfp" in t or "payroll" in t:
        return "Headline jobs data - one of the biggest market movers of the month."
    if "employment change" in t or ("employment" in t and "rate" not in t):
        return "Gauges hiring strength in the labour market."
    if "unemployment rate" in t:
        return "The share of the workforce without a job - a core health check on the economy."
    if "jobless" in t or "claims" in t:
        return "A weekly read on layoffs and labour-market softness."
    if "gdp" in t:
        return "The broadest measure of economic growth."
    if "pmi" in t or "ism" in t:
        return "A business survey that often previews where the economy is heading."
    if "retail sales" in t:
        return "Tracks consumer spending, the engine of most economies."
    if "fomc" in t:
        return "Fed meeting communication - watch for shifts in the policy outlook."
    if "trade balance" in t:
        return "The gap between exports and imports, which feeds into currency demand."
    if "press conf" in t or "speaks" in t or "speech" in t or "testimony" in t:
        return "Officials' commentary can move the currency on tone alone."
    return "A high-impact release that can move the currency."


def direction_note(title, cur):
    t = title.lower()
    tone_driven = any(k in t for k in (
        "rate", "decision", "statement", "fomc", "press conf",
        "speaks", "speech", "testimony"))
    if tone_driven:
        return f"Hawkish signals tend to support {cur}; dovish signals weigh on it."
    return f"A stronger-than-expected print is typically {cur}-positive; a weaker one, {cur}-negative."


def stamp(when):
    """Full dual-tz stamp, e.g. '13:15 UK / 08:15 ET'."""
    return (f"{when.astimezone(TZ):%H:%M} {TZ_LABEL} / "
            f"{when.astimezone(SECOND_TZ):%H:%M} {SECOND_LABEL}")


def stamp_compact(when):
    """Compact dual-tz stamp for dense lists, e.g. '13:15/08:15'."""
    return f"{when.astimezone(TZ):%H:%M}/{when.astimezone(SECOND_TZ):%H:%M}"


def event_block(when, cur, title, forecast, prev):
    bits = []
    if forecast:
        bits.append(f"forecast {forecast}")
    if prev:
        bits.append(f"prev {prev}")
    figures = " (" + ", ".join(bits) + ")" if bits else ""
    body = f"{describe(title)}{figures} {direction_note(title, cur)}"
    return f"\U0001F534 **{stamp(when)} \u00b7 {cur} {title}**\n{body}"


def trump_line(heads):
    flag = "\U0001F1FA\U0001F1F8"
    if not heads:
        return (f"{flag} **Trump Watch:** No significant Trump or "
                f"tariff-related news in the last {LOOKBACK} hours.")
    lines = [f"{flag} **Trump Watch:**"]
    for when, title, _link in heads[:TRUMP_SHOW]:
        lines.append(f"\u2022 {title}")   # source name is kept; links omitted for a clean look
    return "\n".join(lines)


def _pair_name(a, b):
    """Order two currencies into a conventional pair name (base/quote)."""
    ia = PAIR_ORDER.index(a) if a in PAIR_ORDER else 99
    ib = PAIR_ORDER.index(b) if b in PAIR_ORDER else 99
    base, quote = (a, b) if ia <= ib else (b, a)
    return f"{base}/{quote}"


def pairs_schedule(events):
    """Every FX pair touched by red-folder news today, each with its event times."""
    if not events:
        return None
    news_ccys = {cur for _w, cur, *_ in events}
    pairs = set()
    for c in news_ccys:
        if c in PAIR_ORDER:
            for other in PAIR_ORDER:           # pair it against every other major
                if other != c:
                    pairs.add(_pair_name(c, other))
        elif c != "USD":                       # exotic currency (e.g. CNY): pair vs USD
            pairs.add(_pair_name(c, "USD"))

    rows = []
    for p in pairs:
        sides = set(p.split("/"))
        times = sorted(((w, cur) for w, cur, _i, _t, _f, _p in events if cur in sides),
                       key=lambda x: x[0])
        seen_stamps, stamp_list = set(), []
        for w, cur in times:
            s = f"{stamp_compact(w)} ({cur})"
            if s not in seen_stamps:          # collapse two events at the same minute
                seen_stamps.add(s)
                stamp_list.append(s)
        if stamp_list:
            rows.append((len(stamp_list), p, ", ".join(stamp_list)))
    rows.sort(key=lambda r: (-r[0], r[1]))     # busiest pairs first, then alphabetical
    body = "\n".join(f"**{p}** \u2014 {stamps}" for _n, p, stamps in rows)
    return (f"\U0001F4C5 **Pairs in play ({TZ_LABEL} / {SECOND_LABEL}):**\n\n" + body)


def pick_quote(now):
    return QUOTES[now.timetuple().tm_yday % len(QUOTES)]


def build_message(events, heads, now, ev_err="", hd_err=""):
    """Build the embed body (everything except the @everyone greeting line)."""
    events = sorted(events, key=lambda e: e[0])      # always render in time order
    date_str = now.strftime("%A, %B ") + str(now.day) + now.strftime(", %Y")

    sections = [f"\U0001F4F0 **{BRIEF_TITLE}** | {date_str}\n{RULE}"]

    if ev_err:
        sections.append(f"\u26A0\uFE0F Could not load events: {ev_err}")
    elif not events:
        sections.append("_No red-folder (high-impact) events scheduled today._")
    else:
        sections.append("\n\n".join(                 # a blank line between each event
            event_block(when, cur, title, forecast, prev)
            for when, cur, _impact, title, forecast, prev in events))

    sections.append(f"\U0001F1FA\U0001F1F8 **Trump Watch:** \u26A0\uFE0F {hd_err}"
                    if hd_err else trump_line(heads))

    sched = pairs_schedule(events)
    if sched:
        sections.append(sched)

    sections.append(f"_\"{pick_quote(now)}\"_\n\n\u2014 {BRAND}")
    if SIGNOFF:
        sections.append(SIGNOFF)
    return "\n\n".join(sections)                      # one blank line between sections


def chunk_message(text, limit=1900):
    """Split into Discord-sized pieces on blank-line boundaries."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        add = ("\n\n" + block) if cur else block
        if cur and len(cur) + len(add) > limit:
            chunks.append(cur)
            cur = block
        else:
            cur += add
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:                      # split anything still too long on line breaks
        if len(c) <= limit:
            out.append(c)
            continue
        line_cur = ""
        for line in c.split("\n"):
            add = ("\n" + line) if line_cur else line
            if line_cur and len(line_cur) + len(add) > limit:
                out.append(line_cur)
                line_cur = line
            else:
                line_cur += add
        if line_cur:
            out.append(line_cur)
    return out


# --------------------------------------------------------------------------- discord
def _webhook_parts():
    base, _, query = WEBHOOK.partition("?")
    return base.rstrip("/"), query


def post(content, embed_bodies):
    """Post one message: greeting `content` (pings) plus the brief as embed(s). Returns id."""
    base, query = _webhook_parts()
    url = base + "?wait=true" + (("&" + query) if query else "")
    embeds = [{"description": b, "color": EMBED_COLOR} for b in embed_bodies[:10]]
    payload = {
        "username": WEBHOOK_USERNAME,
        "content": content,
        "embeds": embeds,
        # Only @everyone/@here may notify; never ping users or roles from any text.
        "allowed_mentions": {"parse": (["everyone"] if PING_EVERYONE else [])},
    }
    if AVATAR_URL:
        payload["avatar_url"] = AVATAR_URL
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
    if not message_id:
        return
    base, query = _webhook_parts()
    url = f"{base}/messages/{message_id}" + (("?" + query) if query else "")
    req = urllib.request.Request(url, method="DELETE", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:        # 404 = already gone; anything else is real
            raise


def read_ids():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else [str(data)]
    except ValueError:
        return [raw]            # backward-compat with old single-id files


def write_ids(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


# --------------------------------------------------------------------------- main
def main():
    if not WEBHOOK:
        sys.exit("ERROR: set the DISCORD_WEBHOOK_URL environment variable.")

    now = dt.datetime.now(TZ)
    if EXPECTED_HOUR and str(now.hour) != EXPECTED_HOUR:
        print(f"Local hour {now.hour} != EXPECTED_LOCAL_HOUR {EXPECTED_HOUR}; skipping.")
        return

    try:
        events, ev_err = get_economic_events(), ""
    except Exception as e:
        events, ev_err = [], str(e)
    try:
        heads, hd_err = get_trump_headlines(), ""
    except Exception as e:
        heads, hd_err = [], str(e)

    if not POST_IF_EMPTY and not events and not heads and not ev_err and not hd_err:
        print("Nothing to post and POST_IF_EMPTY=0; skipping.")
        return

    body = build_message(events, heads, now, ev_err, hd_err)
    embed_bodies = chunk_message(body, 4096)          # embeds allow up to 4096 chars each

    old = read_ids()
    if old:
        for mid in old:
            delete_message(mid)
        print(f"Deleted {len(old)} previous message(s).")

    new_id = post(GREETING, embed_bodies)
    write_ids([new_id] if new_id else [])
    print(f"Posted brief (1 message, {len(embed_bodies)} embed(s)): "
          f"{len(events)} event(s), {len(heads)} headline(s).")


if __name__ == "__main__":
    main()

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
  POST_HOUR              Post only at/after this local hour.        [5]
  ONCE_PER_DAY           "1" to never post twice in one local day.  [1]
  FORCE_POST             "1" to ignore the time/day gate (manual).  [0]
  STATE_FILE             Stores the ids + last posted date.         [last_message_id.txt]
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
POST_HOUR    = int(os.environ.get("POST_HOUR", "4"))         # post at/after this local hour
ONCE_PER_DAY = os.environ.get("ONCE_PER_DAY", "1") == "1"    # never post twice in one local day
FORCE_POST   = os.environ.get("FORCE_POST", "0") == "1"      # ignore the time/day gate (manual tests)
STATE_FILE = os.environ.get("STATE_FILE", "last_message_id.txt")

GREETING    = os.environ.get("GREETING", "Morning @everyone \U0001F44B")
BRIEF_TITLE = os.environ.get("BRIEF_TITLE", "Forex Morning Brief")
BRAND       = os.environ.get("BRAND", "Team Inner Edge")
SIGNOFF     = os.environ.get("SIGNOFF", "")
WEBHOOK_USERNAME = os.environ.get("WEBHOOK_USERNAME", "TEAM INNER EDGE")
AVATAR_URL  = os.environ.get("AVATAR_URL", "").strip()      # optional logo override (public image URL)
EMBED_COLOR = int(os.environ.get("EMBED_COLOR", "0x5865F2"), 0)  # left-bar colour of the embed
PING_EVERYONE = os.environ.get("PING_EVERYONE", "1") == "1"  # let the greeting actually notify
RULE_LENGTH = int(os.environ.get("RULE_LENGTH", "20"))      # divider bars; fits one mobile line

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

# Trading aphorisms rotated daily (100 -> no repeat within any ~3-month window).
# Signed with BRAND. Original/generic sayings, no attribution, no AI punctuation.
QUOTES = [
    "Cut your losses short and let your winners run.",
    "The trend is your friend until it ends.",
    "Plan the trade, then trade the plan.",
    "Patience is a position.",
    "When in doubt, stay out.",
    "Trade what you see, not what you think.",
    "Discipline beats conviction.",
    "The first loss is the cheapest loss.",
    "Survival comes first, profit comes second.",
    "Risk a little to make a lot, never the reverse.",
    "A small loss today protects tomorrow's account.",
    "Never add to a losing position.",
    "Manage the risk and the profits manage themselves.",
    "Your stop is a promise you keep.",
    "Preparation beats prediction.",
    "Process over outcome, every single day.",
    "Consistency compounds.",
    "The market rewards patience and punishes greed.",
    "No single trade should ever matter that much.",
    "Boredom is part of the edge.",
    "Sit on your hands until the setup is clear.",
    "The best trade is often no trade.",
    "Let the market come to you.",
    "Trade less, focus more.",
    "A clear mind is your sharpest tool.",
    "Emotion is the enemy of execution.",
    "Fear and greed are expensive advisors.",
    "Respect the level, not your opinion.",
    "The chart does not care how you feel.",
    "Hope is not a strategy.",
    "Revenge trading empties accounts.",
    "One good setup beats ten forced ones.",
    "Quality of trades over quantity of trades.",
    "Win the process and the results follow.",
    "Small edges, repeated, build fortunes.",
    "Protect your capital first, and profits will follow.",
    "Capital preserved is opportunity kept.",
    "Cash is a position too.",
    "Missing a trade costs nothing. Chasing one costs plenty.",
    "There is always another setup tomorrow.",
    "The market will be open again next week.",
    "Slow money is real money.",
    "Get rich slow, not broke fast.",
    "Compounding rewards those who stay in the game.",
    "Stay solvent long enough to be right.",
    "The market can test patience longer than you expect.",
    "Trade the plan, not the news.",
    "React to price, not to opinions.",
    "Headlines move fast, trends move slow.",
    "Volatility is opportunity for the prepared.",
    "Plan your entry, your exit, and your risk.",
    "Know your exit before your entry.",
    "An exit plan turns chaos into a checklist.",
    "Define your risk before the trade, not after.",
    "Position size is risk control in disguise.",
    "Leverage cuts both ways.",
    "Overleverage is the fastest road to zero.",
    "Trade small, think big, last long.",
    "The goal is to be around for the next trade.",
    "Protect the downside and the upside takes care of itself.",
    "Confidence comes from preparation, not hope.",
    "Backtest the idea before you risk the account.",
    "Journal every trade, learn from every loss.",
    "Your worst trades teach your best lessons.",
    "Mistakes are tuition, so pay attention.",
    "Review the process, not just the profit.",
    "Edge without discipline is luck waiting to end.",
    "Luck is not a system.",
    "The market humbles the overconfident.",
    "Stay humble or the market will humble you.",
    "Certainty is a warning sign.",
    "The setup you force is the trade you regret.",
    "Wait for the trade to make sense.",
    "If you have to squint, it is not a setup.",
    "Clarity over cleverness.",
    "Simple plans survive volatile days.",
    "Complexity hides risk.",
    "Master one setup before chasing ten.",
    "Depth beats breadth in a trading edge.",
    "Repetition turns a strategy into instinct.",
    "Show up, follow the plan, repeat.",
    "Boring and profitable beats exciting and broke.",
    "The market pays you to wait.",
    "Time in the right trade beats timing every trade.",
    "Trade the session you prepared for.",
    "Respect the news, but trade the chart.",
    "Let red folders sharpen your focus, not your fear.",
    "Volatility days reward plans and punish guesses.",
    "Be early or be patient, never be late.",
    "Chasing price is paying retail for risk.",
    "The trade is not the trader. Stay detached.",
    "You are managing risk, not predicting the future.",
    "Probabilities, not certainties, pay the bills.",
    "Think in bets, not in absolutes.",
    "One trade is noise. A hundred trades are your edge.",
    "Trust the sample size, not the single result.",
    "Green days come from good habits, not good luck.",
    "Risk management is the strategy. Everything else is decoration.",
    "End the day flat in mind, if not in position.",
    "Trade well today, and let the account grow itself.",
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
        return "Measures inflation, a key input for central bank policy."
    if "ppi" in t or "producer price" in t:
        return "Wholesale price pressure that often feeds through into inflation."
    if ("rate" in t and ("decision" in t or "statement" in t)) or "cash rate" in t \
            or "bank rate" in t or "interest rate" in t:
        return "The central bank's interest rate decision, a major driver for the currency."
    if "non-farm" in t or "nonfarm" in t or "nfp" in t or "payroll" in t:
        return "Headline jobs data, one of the biggest market movers of the month."
    if "employment change" in t or ("employment" in t and "rate" not in t):
        return "Gauges hiring strength in the labour market."
    if "unemployment rate" in t:
        return "The share of the workforce without a job, a core health check on the economy."
    if "jobless" in t or "claims" in t:
        return "A weekly read on layoffs and labour-market softness."
    if "gdp" in t:
        return "The broadest measure of economic growth."
    if "pmi" in t or "ism" in t:
        return "A business survey that often previews where the economy is heading."
    if "retail sales" in t:
        return "Tracks consumer spending, the engine of most economies."
    if "fomc" in t:
        return "Fed meeting communication that can shift the policy outlook."
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
        return f"Hawkish signals tend to support {cur}, while dovish signals weigh on it."
    return f"A stronger-than-expected print usually lifts {cur}, and a weaker one weighs on it."


def _t12(when, tz):
    """12-hour am/pm time with no leading zero, e.g. '1:15pm'."""
    return when.astimezone(tz).strftime("%I:%M%p").lower().lstrip("0")


def stamp(when):
    """Full dual-tz stamp, e.g. '1:15pm UK / 8:15am EST'."""
    return f"{_t12(when, TZ)} {TZ_LABEL} / {_t12(when, SECOND_TZ)} {SECOND_LABEL}"


def stamp_compact(when):
    """Compact dual-tz stamp for dense lists, e.g. '1:15pm/8:15am'."""
    return f"{_t12(when, TZ)}/{_t12(when, SECOND_TZ)}"


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
    lines = [f"{flag} **Trump Watch:**", ""]   # blank line before the first bullet
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
    """The day's standout pairs: where BOTH currencies have red-folder news
    (two catalysts = highest volatility). Returns None when there are none, so the
    section is simply omitted rather than restating single-currency events."""
    if not events:
        return None
    majors = sorted({c for _w, c, *_ in events if c in PAIR_ORDER}, key=PAIR_ORDER.index)

    pairs = []
    for i in range(len(majors)):                  # both sides in the news = a "top pair"
        for j in range(i + 1, len(majors)):
            pairs.append(_pair_name(majors[i], majors[j]))
    if not pairs:                                 # fewer than two currencies in play
        return None

    rows = []
    for p in pairs:
        sides = set(p.split("/"))
        times = sorted(((w, cur) for w, cur, _i, _t, _f, _p in events if cur in sides),
                       key=lambda x: x[0])
        seen_stamps, stamp_list = set(), []
        for w, cur in times:
            s = f"{stamp_compact(w)} ({cur})"
            if s not in seen_stamps:              # collapse two events at the same minute
                seen_stamps.add(s)
                stamp_list.append(s)
        if stamp_list:
            rows.append((len(stamp_list), p, ", ".join(stamp_list)))
    if not rows:
        return None
    rows.sort(key=lambda r: (-r[0], r[1]))        # busiest pairs first, then alphabetical
    body = "\n".join(f"**{p}**: {stamps}" for _n, p, stamps in rows)
    return (f"\U0001F3AF **Top pairs today ({TZ_LABEL} / {SECOND_LABEL}):**\n"
            f"_Both currencies in each pair have red-folder news today, so the pair gets "
            f"hit from both sides. Two catalysts on one chart tend to drive the biggest, "
            f"cleanest moves. This is where the day's volatility and opportunity "
            f"are concentrated._\n\n" + body)


def pick_quote(now):
    # toordinal() increments by exactly 1 each calendar day, so consecutive days
    # always get the next quote -- no repeats within len(QUOTES) days, and no
    # year-boundary glitch.
    return QUOTES[now.toordinal() % len(QUOTES)]


def build_message(events, heads, now, ev_err="", hd_err=""):
    """Build the embed body (everything except the @everyone greeting line)."""
    events = sorted(events, key=lambda e: e[0])      # always render in time order
    date_str = now.strftime("%A, %B ") + str(now.day) + now.strftime(", %Y")
    rule = "\u25AC" * RULE_LENGTH                  # fixed length so it never wraps on mobile

    sections = [f"## \U0001F4C8 {BRIEF_TITLE}\n{date_str}\n{rule}"]

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


def read_state():
    """Return {'ids': [...], 'posted_date': 'YYYY-MM-DD' or None}, tolerating old formats."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return {"ids": [], "posted_date": None}
    if not raw:
        return {"ids": [], "posted_date": None}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ids": [raw], "posted_date": None}          # old bare-id file
    if isinstance(data, list):                              # old list-of-ids file
        return {"ids": [str(x) for x in data], "posted_date": None}
    if isinstance(data, dict):
        return {"ids": [str(x) for x in data.get("ids", [])],
                "posted_date": data.get("posted_date")}
    return {"ids": [str(data)], "posted_date": None}


def write_state(ids, posted_date):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": list(ids), "posted_date": posted_date}, f)


# --------------------------------------------------------------------------- main
def main():
    if not WEBHOOK:
        sys.exit("ERROR: set the DISCORD_WEBHOOK_URL environment variable.")

    now = dt.datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = read_state()

    # Gate (skipped for manual/forced runs): wait until at/after POST_HOUR local
    # time, and only post once per local day. This makes the brief land at ~5am UK
    # whenever GitHub actually runs the job -- on time or hours late -- without ever
    # posting twice, no matter how many scheduled runs fire.
    if not FORCE_POST:
        if now.hour < POST_HOUR:
            print(f"Local hour {now.hour} < POST_HOUR {POST_HOUR}; too early, skipping.")
            return
        if ONCE_PER_DAY and state["posted_date"] == today:
            print(f"Already posted today ({today}); skipping.")
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

    old = state["ids"]
    if old:
        for mid in old:
            delete_message(mid)
        print(f"Deleted {len(old)} previous message(s).")

    new_id = post(GREETING, embed_bodies)
    # A forced/manual run posts but does NOT claim the day, so it won't suppress
    # the real scheduled brief; a scheduled run records today's date.
    write_state([new_id] if new_id else [], state["posted_date"] if FORCE_POST else today)
    print(f"Posted brief (1 message, {len(embed_bodies)} embed(s)): "
          f"{len(events)} event(s), {len(heads)} headline(s).")


if __name__ == "__main__":
    main()

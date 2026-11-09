#! /usr/bin/env python
# -*- coding: utf-8 -*-
# apple_events.py
# Apple Events discovery for the Calendars & Reminders plugin.
# Author: Durosity
#
# This module deliberately has NO dependency on `indigo` or `EventKit` so the
# parsing and planning logic can be unit-tested anywhere. plugin.py owns the
# EventKit side (actually writing to Calendar.app) and the Indigo device states.
#
# Pipeline:
#   fetch_feed()        -> list of FeedItem
#   find_candidates()   -> list of Candidate (confirmed / rumoured, one per date)
#   plan_actions()      -> list of PlannedAction (create / upgrade) vs saved state
#   (plugin.py executes the plan against EventKit and records the result)

import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

KIND_CONFIRMED = "confirmed"
KIND_RUMOURED = "rumoured"

DEFAULT_FEED_URL = "https://feeds.macrumors.com/MacRumors-All"
DEFAULT_EVENT_NAME = "Apple Event"
DEFAULT_INCLUDE_KEYWORDS = ("event, keynote, launch, unveil, announce, debut, reveal, "
                            "coming on, arrives, release date, on sale")
DEFAULT_EXCLUDE_KEYWORDS = ("beta, deal, deals, sale, discount, % off, giveaway, review, "
                            "hands-on, how to, tips, comparison, vs, wallpaper")

# A confirmed event is only given a start time when the story states one WITH a
# timezone ("10:00 a.m. Pacific Time"). A bare "9:00 a.m." could be anyone's local
# time, and some launches (a press-release drop) have no event time at all, so we
# never guess: those become all-day entries and the notes say the time is unknown.
EVENT_DURATION = timedelta(hours=1)

# A date with no year ("September 9") is only believed if it falls within this
# many days after the article was published. Otherwise a story about an event
# that already happened would be mis-read as the same date next year.
MAX_LEAD_DAYS = 240

# For a RUMOUR whose date is only in the article body (not the headline), the
# date must be in the opening lines AND have launch wording right next to it.
# Without this, incidental dates ("stores start installing displays on Oct 8",
# "goes on sale in China Oct 23") were being picked up.
LEAD_WINDOW_CHARS = 320
HEDGE_WINDOW_CHARS = 400     # where we look for "reportedly / plans to / according to"
PROXIMITY_CHARS = 90

USER_AGENT = "Mozilla/5.0 (compatible; Indigo-Calendars-Reminders-Plugin/4.2.10)"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FeedItem:
    title: str
    link: str
    published: Optional[datetime]   # timezone-aware, or None if unparseable
    text: str                       # plain text of the summary/description


@dataclass
class Config:
    event_name: str = DEFAULT_EVENT_NAME
    include_rumoured: bool = False
    rumoured_prefix: str = ""
    rumoured_suffix: str = ""
    days_ahead: int = 90
    include_keywords: List[str] = field(default_factory=list)
    exclude_keywords: List[str] = field(default_factory=list)


@dataclass
class Candidate:
    kind: str                        # KIND_CONFIRMED / KIND_RUMOURED
    event_date: date                 # the day of the event (Pacific for confirmed)
    start: Optional[datetime]        # aware datetime for confirmed events, else None
    all_day: bool
    tagline: str                     # e.g. "Surprise and Shine" (confirmed only)
    headline: str
    url: str
    time_assumed: bool = False       # (unused: start times are never assumed)
    wwdc: bool = False

    @property
    def key(self) -> str:
        return self.event_date.isoformat()


@dataclass
class PlannedAction:
    op: str                          # "create" or "upgrade"
    key: str
    candidate: Candidate
    existing_entry: Optional[dict] = None   # the saved state entry being upgraded


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def split_keywords(value: str) -> List[str]:
    """Comma-separated keyword string -> lowercase list (empty items dropped)."""
    return [k.strip().lower() for k in (value or "").split(",") if k.strip()]


def _keyword_regex(keywords: List[str]):
    """Compile keywords into one regex, matching whole words/phrases only."""
    if not keywords:
        return None
    parts = [r"(?<![\w])" + re.escape(k) + r"(?![\w])" for k in keywords]
    return re.compile("|".join(parts), re.IGNORECASE)


def config_from_props(props) -> Config:
    """Build a Config from an Indigo device's pluginProps (any mapping works)."""
    def _int(name, default):
        try:
            return int(props.get(name, default))
        except (TypeError, ValueError):
            return default

    def _bool(name):
        v = props.get(name, False)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return bool(v)

    return Config(
        event_name=(props.get("eventName") or DEFAULT_EVENT_NAME).strip() or DEFAULT_EVENT_NAME,
        include_rumoured=_bool("includeRumoured"),
        rumoured_prefix=(props.get("rumouredPrefix") or "").strip(),
        rumoured_suffix=(props.get("rumouredSuffix") or "").strip(),
        days_ahead=_int("daysAhead", 90),
        include_keywords=split_keywords(props.get("includeKeywords", DEFAULT_INCLUDE_KEYWORDS)),
        exclude_keywords=split_keywords(props.get("excludeKeywords", DEFAULT_EXCLUDE_KEYWORDS)),
    )


# ---------------------------------------------------------------------------
# Feed fetching / parsing
# ---------------------------------------------------------------------------

def strip_html(markup: str) -> str:
    """Very small HTML -> text helper (feed descriptions only)."""
    if not markup:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    # MacRumors wraps product names in U+200C (zero width non-joiner) to block
    # auto-linking; remove those and other invisible characters.
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pubdate(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)          # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))   # Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(xml_bytes: bytes) -> List[FeedItem]:
    """Parse an RSS 2.0 or Atom feed into FeedItems."""
    root = ET.fromstring(xml_bytes)
    items: List[FeedItem] = []

    def local(tag):
        return tag.split("}")[-1]

    for node in root.iter():
        name = local(node.tag)
        if name not in ("item", "entry"):
            continue
        fields = {}
        link = ""
        for child in node:
            cname = local(child.tag)
            if cname == "link":
                link = child.get("href") or (child.text or "").strip() or link
            elif cname not in fields:
                fields[cname] = (child.text or "").strip()
        body = fields.get("encoded") or fields.get("description") or fields.get("content") \
            or fields.get("summary") or ""
        items.append(FeedItem(
            title=strip_html(fields.get("title", "")),
            link=link,
            published=_parse_pubdate(fields.get("pubDate") or fields.get("published")
                                     or fields.get("updated")),
            text=strip_html(body),
        ))
    return items


def fetch_feed(url: str, timeout: int = 30) -> List[FeedItem]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return parse_feed(response.read())


# ---------------------------------------------------------------------------
# Date / time extraction
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_PAT = (r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
              r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_WEEKDAY_PAT = (r"(?:(?P<wd>Mon|Tues?|Wed(?:nes)?|Thu(?:rs?)?|Fri|Sat(?:ur)?|Sun)"
                r"(?:day)?\.?,?\s+)?")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# "Tuesday, October 13" / "October 13th, 2026" / "Oct. 13"
_DATE_MDY = re.compile(
    _WEEKDAY_PAT + _MONTH_PAT + r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    r"(?:,?\s+(?P<year>20\d{2})(?!\d))?")
# "Tuesday 13 October" / "13th of October 2026"
_DATE_DMY = re.compile(
    _WEEKDAY_PAT + r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_PAT +
    r"(?![a-z])(?:,?\s+(?P<year>20\d{2})(?!\d))?")


def _build_date(mon: str, day: str, year: Optional[str], wd: Optional[str],
                ref: date) -> Optional[date]:
    """Resolve a month/day (+ optional year/weekday) to a concrete date.

    Without a year we choose the occurrence closest after `ref` (the article's
    publish date). If a weekday was written, it must agree with the date; that
    guards against picking the wrong year.
    """
    month = _MONTHS[mon[:3].lower()]
    d = int(day)
    wd_idx = _WEEKDAYS[wd[:3].lower()] if wd else None

    years = [int(year)] if year else [ref.year, ref.year + 1]
    for y in years:
        try:
            candidate = date(y, month, d)
        except ValueError:
            continue
        if not year and candidate < ref - timedelta(days=7):
            continue        # earlier than the article by more than a week: try next year
        if not year and candidate > ref + timedelta(days=MAX_LEAD_DAYS):
            continue        # too far ahead to be believable without an explicit year
        if wd_idx is not None and candidate.weekday() != wd_idx:
            continue
        return candidate
    return None


def find_dates(text: str, ref: date) -> List[tuple]:
    """Return [(date, start_index)] for every plausible calendar date in `text`."""
    found = []
    for pattern in (_DATE_MDY, _DATE_DMY):
        for m in pattern.finditer(text):
            # Skip "May 15%" / "October 13 percent" style false positives.
            tail = text[m.end():m.end() + 9].lower()
            if tail.lstrip().startswith(("%", "percent")):
                continue
            resolved = _build_date(m.group("mon"), m.group("day"), m.group("year"),
                                   m.group("wd"), ref)
            if resolved:
                found.append((resolved, m.start()))
    found.sort(key=lambda pair: pair[1])
    # De-duplicate identical dates while preserving order of first appearance
    seen, unique = set(), []
    for d, pos in found:
        if d not in seen:
            seen.add(d)
            unique.append((d, pos))
    return unique


_TIME_RE = re.compile(
    r"(?<![\d:])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap])\.?m\.?"
    r"(?:\s*\(?(?P<tz>Pacific(?:\s+(?:Standard\s+|Daylight\s+)?Time)?|PT|PDT|PST|"
    r"Eastern(?:\s+(?:Standard\s+|Daylight\s+)?Time)?|ET|EDT|EST|GMT|UTC|BST)\b)?",
    re.IGNORECASE)

_TZ_MAP = {
    "pacific": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "eastern": "America/New_York", "et": "America/New_York",
    "edt": "America/New_York", "est": "America/New_York",
    "gmt": "UTC", "utc": "UTC", "bst": "Europe/London",
}


def find_start_time(text: str):
    """Find the first 'h[:mm] a.m./p.m. <timezone>' in `text`.

    A time WITHOUT a timezone is ignored (see the note on EVENT_DURATION).
    Returns (time_of_day, tz_name) or None.
    """
    for m in _TIME_RE.finditer(text):
        if not m.group("tz"):
            continue
        hour = int(m.group("h"))
        minute = int(m.group("m") or 0)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            continue
        if m.group("ap").lower() == "p" and hour != 12:
            hour += 12
        elif m.group("ap").lower() == "a" and hour == 12:
            hour = 0
        tz_name = _TZ_MAP.get(m.group("tz").split()[0].lower())
        if tz_name:
            return time(hour, minute), tz_name
    return None


def _zone(name: str):
    """tzinfo for an IANA name, or None if the system can't provide it.

    Never fall back to UTC: a confirmed 10:00 Pacific keynote silently landing at
    10:00 UTC would be hours out. Callers degrade to an all-day entry instead.
    """
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_APPLEISH_RE = re.compile(r"\b(apple|wwdc|iphone|ipad|mac|macbook|imac|airpods|homepod|"
                          r"watch|vision pro|siri|ios)\b", re.IGNORECASE)

# A headline that states the event as fact: "Apple Event Announced for ...",
# "Apple Announces September 9 Event", "WWDC 2026 Keynote Set for June 8" ...
_NOUN = r"(?:event|keynote|wwdc|(?:product\s+)?launch|presentation|announcement)"
_CONFIRMED_RE = re.compile(
    r"(?:\b" + _NOUN + r"\b.*\b(?:announced|confirmed|set for|scheduled|invites?|official)\b)"
    r"|(?:\b(?:announces?|confirms?|sends? out|sent out)\b.*\b(?:" + _NOUN[3:-1] + r"|invites?)\b)"
    r"|(?:\binvites? (?:go|went) out\b)",
    re.IGNORECASE)

# Backup signal for headlines we don't anticipate: the story's first sentence says
# Apple itself announced an event/launch ("Apple today announced an upcoming product
# launch that will take place on ...").
_LEAD_CONFIRM_RE = re.compile(
    r"\bApple (?:today |has |just )?(?:announced|confirmed)\b[^.]{0,80}"
    r"\b(?:event|keynote|launch|presentation)\b", re.IGNORECASE)

# Hedging in the headline means it is NOT official, however it is phrased.
_HEDGE_RE = re.compile(
    r"\b(rumou?r(?:ed|s)?|reportedly|expected|likely|could|may(?!\s+\d)|might|plans?|planning|"
    r"according to|said to|tipped|leak(?:s|ed)?|gurman|kuo|potential(?:ly)?|"
    r"what to expect|when (?:will|is))\b",
    re.IGNORECASE)

_TAGLINE_RE = re.compile(r":\s*['\u2018\u201c\"]([^'\u2018\u2019\u201c\u201d\"]{3,60})['\u2019\u201d\"]\s*$")


def _tagline_from_title(title: str) -> str:
    m = _TAGLINE_RE.search(title)
    return m.group(1).strip() if m else ""


def _near_trigger(text: str, pos: int, trigger_re) -> bool:
    """True if a trigger keyword appears within PROXIMITY_CHARS of `pos`."""
    window = text[max(0, pos - PROXIMITY_CHARS): pos + PROXIMITY_CHARS]
    return bool(trigger_re.search(window))


def classify(item: FeedItem, cfg: Config, today: date) -> Optional[Candidate]:
    """Turn one feed item into a Candidate, or None if it isn't an event."""
    title = item.title
    if not title:
        return None

    exclude_re = _keyword_regex(cfg.exclude_keywords)
    if exclude_re and exclude_re.search(title):
        return None

    ref = (item.published.date() if item.published else today)
    # The look-ahead window only limits RUMOURS (far-off rumoured dates are
    # unreliable). Official announcements are accepted up to a year out because
    # Apple announces WWDC around ten weeks ahead.
    rumour_horizon = today + timedelta(days=max(cfg.days_ahead, 1))
    confirmed_horizon = today + timedelta(days=365)
    lead = item.text[:600]

    # -------- Confirmed: factual headline (no hedging) naming a date --------
    confirmed_signal = bool(_CONFIRMED_RE.search(title)
                            or _LEAD_CONFIRM_RE.search(item.text[:250]))
    if confirmed_signal and not _HEDGE_RE.search(title) \
            and _APPLEISH_RE.search(title + " " + lead[:200]):
        title_dates = find_dates(title, ref) or find_dates(lead, ref)
        if title_dates:
            event_date = title_dates[0][0]
            if (today - timedelta(days=1) <= event_date <= confirmed_horizon
                    and event_date <= ref + timedelta(days=MAX_LEAD_DAYS)):
                found = find_start_time(lead)          # needs an explicit timezone
                zone = _zone(found[1]) if found else None
                start = (datetime.combine(event_date, found[0], tzinfo=zone)
                         if (found and zone is not None) else None)
                return Candidate(
                    kind=KIND_CONFIRMED, event_date=event_date, start=start,
                    all_day=start is None,      # no stated time: date only, never a guessed time
                    tagline=_tagline_from_title(title),
                    headline=title, url=item.link,
                    wwdc=bool(re.search(r"\bWWDC\b", title)),
                )

    # -------- Rumoured: launch-ish headline with a concrete future date -----
    # A "rumour" must actually read like one. Plain factual stories that happen to
    # mention a date (a store reopening, say) are not rumours, and labelling them
    # as such would be wrong - Apple may well have confirmed that date itself.
    include_re = _keyword_regex(cfg.include_keywords)
    hedged = bool(_HEDGE_RE.search(title + " " + item.text[:HEDGE_WINDOW_CHARS]))
    if include_re and hedged and include_re.search(title):
        dates = find_dates(title, ref)
        if not dates:
            window = item.text[:LEAD_WINDOW_CHARS]
            dates = [(d, pos) for d, pos in find_dates(window, ref)
                     if _near_trigger(window, pos, include_re)]
        for event_date, _pos in dates:
            if today <= event_date <= rumour_horizon:
                return Candidate(
                    kind=KIND_RUMOURED, event_date=event_date, start=None,
                    all_day=True, tagline="", headline=title, url=item.link,
                )
    return None


def find_candidates(items: List[FeedItem], cfg: Config, today: date) -> List[Candidate]:
    """Classify every item and collapse to one Candidate per event date.

    A confirmed candidate always beats a rumoured one for the same date; between
    two of the same kind the earliest article (first seen) wins.
    """
    by_date: Dict[str, Candidate] = {}
    # Oldest first so "first seen" is stable
    ordered = sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc))
    for item in ordered:
        cand = classify(item, cfg, today)
        if cand is None:
            continue
        existing = by_date.get(cand.key)
        if existing is None or (existing.kind == KIND_RUMOURED and cand.kind == KIND_CONFIRMED):
            by_date[cand.key] = cand
    return sorted(by_date.values(), key=lambda c: c.event_date)


# ---------------------------------------------------------------------------
# Titles and notes
# ---------------------------------------------------------------------------

def build_title(cand: Candidate, cfg: Config) -> str:
    if cand.kind == KIND_CONFIRMED:
        base = "WWDC Keynote" if cand.wwdc else cfg.event_name
        return f"{base}: {cand.tagline}" if cand.tagline else base
    parts = [cfg.rumoured_prefix, cfg.event_name, cfg.rumoured_suffix]
    return " ".join(p for p in parts if p)


def build_notes(cand: Candidate) -> str:
    lines = []
    if cand.kind == KIND_RUMOURED:
        lines.append("RUMOURED - this date has not been confirmed by Apple.")
    else:
        lines.append("Confirmed by Apple.")
        if cand.all_day:
            lines.append("Start time unknown (not stated yet) - check Apple's announcement.")
    lines.append(f"Source: {cand.headline}")
    if cand.url:
        lines.append(cand.url)
    lines.append("Added by the Indigo Calendars & Reminders plugin (Apple Events).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State (what we have already put in the calendar)
# ---------------------------------------------------------------------------

def empty_state() -> dict:
    return {"version": 1, "entries": {}}


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("entries"), dict):
            return state
    except (OSError, ValueError):
        pass
    return empty_state()


def save_state(path: str, state: dict) -> None:
    """Atomic write so a crash mid-save can't corrupt the file."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".appleEvents.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def prune_state(state: dict, today: date, keep_days: int = 365) -> None:
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    for key in [k for k in state["entries"] if k < cutoff]:
        del state["entries"][key]


def plan_actions(state: dict, candidates: List[Candidate], include_rumoured: bool,
                 ek_exists=lambda key, entry: True) -> List[PlannedAction]:
    """Decide what to do for each candidate, given what we did previously.

    ek_exists(key, entry) -> bool tells us whether the calendar event we created
    for that saved entry still exists. If a user deleted a *rumoured* entry we
    respect that and don't re-create it. A *confirmed* announcement is new
    information, so it is created even if the earlier rumour was deleted.
    """
    plan: List[PlannedAction] = []
    for cand in candidates:
        entry = state["entries"].get(cand.key)

        if entry is None:
            if cand.kind == KIND_RUMOURED and not include_rumoured:
                continue
            plan.append(PlannedAction("create", cand.key, cand))
            continue

        if entry.get("kind") == KIND_RUMOURED and cand.kind == KIND_CONFIRMED:
            if ek_exists(cand.key, entry):
                plan.append(PlannedAction("upgrade", cand.key, cand, existing_entry=entry))
            else:
                plan.append(PlannedAction("create", cand.key, cand))
        # Same kind already tracked (or already confirmed): nothing to do.
    return plan


def record_result(state: dict, action: PlannedAction, ek_id: str, title: str,
                  now: Optional[datetime] = None) -> None:
    cand = action.candidate
    now = now or datetime.now()
    previous = state["entries"].get(action.key, {})
    state["entries"][action.key] = {
        "kind": cand.kind,
        "ek_id": ek_id,
        "title": title,
        "headline": cand.headline,
        "url": cand.url,
        "all_day": cand.all_day,
        "start": cand.start.isoformat() if cand.start else None,
        "created": previous.get("created", now.isoformat(timespec="seconds")),
        "updated": now.isoformat(timespec="seconds"),
    }


def next_event(state: dict, today: date) -> Optional[dict]:
    """Soonest tracked entry on or after today, with its date key attached."""
    upcoming = sorted(k for k in state["entries"] if k >= today.isoformat())
    if not upcoming:
        return None
    entry = dict(state["entries"][upcoming[0]])
    entry["date"] = upcoming[0]
    return entry

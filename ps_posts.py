"""Signs in to ParentSquare and turns recent posts into calendar events.

Credentials come from the PS_EMAIL and PS_PASSWORD environment variables
(GitHub repository secrets). Nothing is written to disk except the built site,
which contains post titles, links and the dated lines found in posts, never the
login or the full post text.
"""
import datetime as dt
import hashlib
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.parentsquare.com"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAY = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?"
NUM_DATE = r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?"
NAME_DATE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"
# A numeric date only counts at the start of a line or right after a weekday,
# so fractions like "4/12" in a math note are not mistaken for dates.
LEAD_NUM = re.compile(rf"^\s*(?:{WEEKDAY}\s*)?{NUM_DATE}((?:\s*(?:&|and|,|-)\s*\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)*)", re.I)
WEEKDAY_NUM = re.compile(rf"\b{WEEKDAY}\s+{NUM_DATE}", re.I)
NAME_RE = re.compile(rf"\b(?:{WEEKDAY}\s+)?{NAME_DATE}\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*(?:-|–|to)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d/])|\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?|\b(\d{1,2})\s*(am|pm)\b", re.I)
NOISE = re.compile(r"^\[|day \d schedule|^\s*(posted|updated)\b", re.I)


class LoginError(Exception):
    pass


def login(email, password):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (school-calendar; personal use)"
    page = s.get(f"{BASE}/signin", timeout=30)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    form = soup.find("form", action="/sessions")
    if not form:
        raise LoginError("ParentSquare's sign-in page changed (no sign-in form found).")
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    data["session[email]"] = email
    data["session[password]"] = password
    r = s.post(f"{BASE}/sessions", data=data, timeout=30)
    r.raise_for_status()
    if "/signin" in r.url or 'name="session[password]"' in r.text:
        raise LoginError("ParentSquare did not accept the saved email/password.")
    if re.search(r"verification code|verify your", r.text, re.I) and "feed" not in r.url:
        raise LoginError("ParentSquare asked for a verification code, so the automatic sign-in can't finish.")
    return s


BLOCKS = {"p", "div", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "section"}


def block_text(node):
    """Text with line breaks only between block elements, like a browser shows it."""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for tag in node.find_all(BLOCKS):
        tag.insert_after("\n")
    return node.get_text("")


def _post_ids(session, school_id, pages):
    ids = []
    for page in range(1, pages + 1):
        r = session.get(f"{BASE}/schools/{school_id}/feeds", params={"page": page}, timeout=30)
        if r.status_code != 200 or "/signin" in r.url:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        found = [e["id"].replace("feed-title-", "") for e in soup.select('[id^="feed-title-"]')]
        if not found:
            break
        ids.extend(i for i in found if i not in ids)
        time.sleep(0.5)
    return ids


def _read_post(session, post_id):
    r = session.get(f"{BASE}/feeds/{post_id}", timeout=30)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    box = soup.select_one(".feed-show")
    if not box:
        return None
    ts = box.select_one(".time-ago[data-timestamp]")
    subject = box.select_one(".subject")
    desc = box.select_one(".description")
    audience = box.select_one(".feed-metadata-time .text-dark")
    if not (ts and subject):
        return None
    text = block_text(desc) if desc else ""
    return {
        "id": post_id,
        "title": " ".join(subject.get_text(" ").split()),
        "posted": dt.datetime.fromisoformat(ts["data-timestamp"].replace(" ", "T", 1)),
        "audience": " ".join(audience.get_text(" ").split()) if audience else "",
        "lines": [" ".join(x.split()) for x in text.split("\n") if x.strip()],
        "link": f"{BASE}/feeds/{post_id}",
    }


def _resolve(month, day, year, posted):
    try:
        if year:
            y = int(year)
            y = y + 2000 if y < 100 else y
            return dt.date(y, month, day)
        best = None
        for y in (posted.year - 1, posted.year, posted.year + 1):
            d = dt.date(y, month, day)
            delta = (d - posted).days
            if -45 <= delta <= 300 and (best is None or abs(delta) < abs((best - posted).days)):
                best = d
        return best
    except ValueError:
        return None


def _times(text):
    def hhmm(h, m, ap, fallback_pm):
        h = int(h)
        m = int(m or 0)
        ap = (ap or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and fallback_pm and 1 <= h <= 7:
            h += 12
        return dt.time(h % 24, m)
    m = TIME_RE.search(text)
    if not m:
        return None, None
    g = m.groups()
    try:
        if g[0] and not (g[1] or g[2] or g[4] or g[5]):
            return None, None  # "2 - 11" without a colon or am/pm is not a time
        if g[0]:  # a range like 6:00pm - 8:00pm or 5:00 - 8:00
            end = hhmm(g[3], g[4], g[5], True)
            start = hhmm(g[0], g[1], g[2] or (g[5] if int(g[0]) <= int(g[3]) else None), True)
            if end <= start and end.hour < 12:
                end = dt.time(end.hour + 12, end.minute)
            return start, end
        if g[6]:
            return hhmm(g[6], g[7], g[8], True), None
        return hhmm(g[9], None, g[10], False), None
    except ValueError:
        return None, None


def _blank(text, spans):
    for a, b in sorted(spans, reverse=True):
        text = text[:a] + " " * (b - a) + text[b:]
    return text


def _clean_title(text, spans):
    first = min(a for a, _ in spans) if spans else 0
    lead = re.match(rf"^\s*(?:{WEEKDAY})?\s*", text, re.I).end()
    if spans and first > lead + 2:
        # The date sits inside a sentence; keep the sentence readable.
        return re.sub(r"\s{2,}", " ", text).strip().rstrip(".")
    for a, b in sorted(spans, reverse=True):
        text = text[:a] + " " + text[b:]
    text = re.sub(r"\(\s*\)", "", text)
    text = TIME_RE.sub("", text)
    text = re.sub(rf"^\s*(?:{WEEKDAY})\s*", "", text, flags=re.I)
    text = re.sub(r"^[\s:\-–—,&@|•]+|[\s:\-–—,&@|•]+$", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def events_from_line(line, posted, is_title=False):
    """Returns [(date, title, start_time, end_time)] for dates found in one line."""
    if len(line) > 160 or NOISE.search(line):
        return []
    found, spans = [], []
    m = LEAD_NUM.match(line)
    if m:
        d = _resolve(int(m.group(1)), int(m.group(2)), m.group(3), posted)
        if d:
            found.append(d)
        for extra in re.finditer(NUM_DATE, m.group(4) or ""):
            d2 = _resolve(int(extra.group(1)), int(extra.group(2)), extra.group(3), posted)
            if d2:
                found.append(d2)
        spans.append(m.span())
    else:
        for wm in WEEKDAY_NUM.finditer(line):
            d = _resolve(int(wm.group(1)), int(wm.group(2)), wm.group(3), posted)
            if d:
                found.append(d)
                spans.append(wm.span())
        for nm in NAME_RE.finditer(line):
            d = _resolve(MONTHS[nm.group(1).lower()[:3]], int(nm.group(2)), nm.group(3), posted)
            if d:
                found.append(d)
                spans.append(nm.span())
    if not found and is_title:
        low = line.lower()
        if "tomorrow" in low:
            found.append(posted + dt.timedelta(days=1))
        elif re.search(r"\b(tonight|today)\b", low):
            found.append(posted)
    if not found:
        return []
    title = _clean_title(line, spans)
    if is_title:
        title = re.sub(r"\b(tomorrow|tonight|today)\b!?", "", title, flags=re.I)
        title = re.sub(r"\s{2,}", " ", re.sub(r"^[\s:\-–—,&@|•!📢]+|[\s:\-–—,&@|•]+$", "", title)).strip() or line
    if len(re.sub(r"[^A-Za-z]", "", title)) < 3:
        return []
    start, end = _times(_blank(line, spans))
    return [(d, title, start, end) for d in sorted(set(found))]


def fetch_posts(email, password, feeds, tz, days_back=45, pages=4):
    """Returns (posts, events). feeds: [{school_id, group, name}]."""
    session = login(email, password)
    cutoff = dt.datetime.now(tz) - dt.timedelta(days=days_back)
    seen = {}
    for feed in feeds:
        for pid in _post_ids(session, feed["school_id"], pages):
            if pid in seen:
                if feed["group"] not in seen[pid]["groups"]:
                    seen[pid]["groups"].append(feed["group"])
                continue
            post = _read_post(session, pid)
            time.sleep(0.4)
            if not post:
                continue
            post["groups"] = [feed["group"]]
            seen[pid] = post
            if post["posted"].astimezone(tz) < cutoff:
                break  # feeds are newest-first; the rest are older
    posts = sorted(seen.values(), key=lambda p: p["posted"], reverse=True)

    events = []
    for p in posts:
        posted = p["posted"].astimezone(tz).date()
        groups = ["district"] if len(p["groups"]) > 1 else p["groups"]
        candidates = [c for c in events_from_line(p["title"], posted, is_title=True)
                      # a title dated the day it was posted is a newsletter date, not an event
                      if c[0] != posted or re.search(r"\b(tonight|today)\b", p["title"], re.I)]
        for line in p["lines"]:
            candidates += events_from_line(line, posted)
        for d, title, start, end in candidates:
            if d < posted - dt.timedelta(days=1):
                continue  # a recap of something that already happened
            ev = {"title": title, "groups": groups, "link": p["link"], "via": "post",
                  "postTitle": p["title"], "posted": p["posted"].astimezone(tz).isoformat(timespec="minutes"),
                  "location": None, "info": None}
            if start:
                s = dt.datetime.combine(d, start, tz)
                e = dt.datetime.combine(d, end, tz) if end and end > start else s + dt.timedelta(hours=1)
                ev.update(start=s.isoformat(timespec="minutes"), end=e.isoformat(timespec="minutes"), allDay=False)
            else:
                ev.update(start=d.isoformat(), end=(d + dt.timedelta(days=1)).isoformat(), allDay=True)
            ev["uid"] = f"ps-{p['id']}-{d.isoformat()}-{hashlib.md5(title.encode()).hexdigest()[:8]}"
            events.append(ev)
    post_list = [{"title": p["title"], "link": p["link"], "groups": ["district"] if len(p["groups"]) > 1 else p["groups"],
                  "posted": p["posted"].astimezone(tz).isoformat(timespec="minutes")}
                 for p in posts if p["posted"].astimezone(tz) >= cutoff]
    return post_list, events

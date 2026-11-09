#!/usr/bin/env python3
"""Builds the school calendar site.

Pulls events from:
  * the district's and schools' public Google calendars (no login needed), and
  * the ParentSquare calendar subscription feeds (private link; the token comes
    from the PS_CALENDAR_TOKEN environment variable / GitHub secret).
Writes docs/index.html, docs/events.json and docs/calendar.ics.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
TZ = ZoneInfo(CONFIG["timezone"])
OUT = ROOT / "docs"
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "school-calendar-builder/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def school_year_window(today):
    m = CONFIG.get("school_year_start_month", 8)
    start_year = today.year if today.month >= m else today.year - 1
    start = dt.date(start_year, m, 1)
    # Show the current school year plus the next few months, so summer
    # announcements for the following year still appear.
    end = dt.date(start_year + 1, m, 1) + dt.timedelta(days=120)
    return start, end


def google_eid(event_id, cal_id):
    short = cal_id.replace("@group.calendar.google.com", "@g")
    raw = f"{event_id} {short}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def to_local(value):
    """Returns (iso string, all_day bool)."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ).isoformat(timespec="minutes"), False
    return value.isoformat(), True


def instance_id(uid, comp):
    """Google's id for one occurrence of a recurring event."""
    start = comp.get("DTSTART").dt
    if isinstance(start, dt.datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        suffix = start.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    else:
        suffix = start.strftime("%Y%m%d")
    return f"{uid}_{suffix}"


def first_link(description):
    if not description:
        return None
    m = URL_RE.search(html.unescape(str(description)))
    return m.group(0).rstrip(".,;") if m else None


def parse_events(ics_bytes, start, end):
    cal = icalendar.Calendar.from_ical(ics_bytes)
    series = {str(c.get("UID", "")).replace("@google.com", "")
              for c in cal.walk("VEVENT") if c.get("RRULE") is not None}
    out = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        uid = str(comp.get("UID", "")).replace("@google.com", "")
        recurring = uid in series
        s, all_day = to_local(comp.get("DTSTART").dt)
        e_raw = comp.get("DTEND")
        e = to_local(e_raw.dt)[0] if e_raw else s
        out.append({
            "uid": uid,
            "gid": instance_id(uid, comp) if recurring else uid,
            "title": str(comp.get("SUMMARY", "(untitled)")).strip(),
            "start": s,
            "end": e,
            "allDay": all_day,
            "location": str(comp.get("LOCATION", "")).strip() or None,
            "info": first_link(comp.get("DESCRIPTION")),
        })
    return out


def main():
    today = dt.datetime.now(TZ).date()
    start, end = school_year_window(today)
    events = {}
    problems = []

    for gc in CONFIG["google_calendars"]:
        url = ("https://calendar.google.com/calendar/ical/"
               + urllib.parse.quote(gc["id"]) + "/public/basic.ics")
        try:
            items = parse_events(fetch(url), start, end)
        except Exception as exc:  # keep going if one source fails
            problems.append(f"{gc['name']}: {exc}")
            continue
        for ev in items:
            key = (ev["uid"], ev["start"])
            ev["groups"] = [gc["group"]]
            ev["link"] = "https://www.google.com/calendar/event?eid=" + google_eid(ev["gid"], gc["id"])
            ev["source"] = gc["name"]
            events[key] = ev
        print(f"{gc['name']}: {len(items)} events")

    token = os.environ.get("PS_CALENDAR_TOKEN", "").strip()
    if token:
        for ps in CONFIG["parentsquare_feeds"]:
            url = f"https://www.parentsquare.com/schools/{ps['school_id']}/users/{token}/calendar.ics"
            try:
                items = parse_events(fetch(url), start, end)
            except Exception as exc:
                problems.append(f"ParentSquare {ps['name']}: {exc}")
                continue
            added = 0
            for ev in items:
                key = (ev["uid"], ev["start"])
                if key in events:
                    if ps["group"] not in events[key]["groups"] and events[key]["groups"] != ["district"]:
                        events[key]["groups"].append(ps["group"])
                    continue
                ev["groups"] = [ps["group"]]
                ev["link"] = f"https://www.parentsquare.com/schools/{ps['school_id']}/calendars"
                ev["source"] = "ParentSquare – " + ps["name"]
                events[key] = ev
                added += 1
            print(f"ParentSquare {ps['name']}: {len(items)} events ({added} not in Google calendars)")
    else:
        print("PS_CALENDAR_TOKEN not set; skipping ParentSquare feeds")

    if not events:
        print("No events fetched; leaving the existing site untouched.", *problems, sep="\n")
        sys.exit(1)

    ordered = sorted(events.values(), key=lambda e: (e["start"], e["title"]))
    for e in ordered:
        e.pop("gid", None)
    now = dt.datetime.now(TZ)
    payload = {
        "title": CONFIG["site_title"],
        "updated": now.isoformat(timespec="minutes"),
        "groups": CONFIG["groups"],
        "problems": problems,
        "events": ordered,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    template = (ROOT / "template.html").read_text()
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    (OUT / "index.html").write_text(template.replace("/*__DATA__*/null", data))
    write_ics(ordered)
    (OUT / ".nojekyll").write_text("")
    print(f"Wrote {len(ordered)} events. Problems: {problems or 'none'}")


def write_ics(ordered):
    cal = icalendar.Calendar()
    cal.add("prodid", "-//school-calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", CONFIG["site_title"])
    cal.add("x-wr-timezone", CONFIG["timezone"])
    for e in ordered:
        ev = icalendar.Event()
        ev.add("uid", f"{e['uid']}-{e['start']}@school-calendar")
        labels = [CONFIG["groups"][g]["short"] for g in e["groups"]]
        ev.add("summary", f"[{'/'.join(labels)}] {e['title']}")
        if e["allDay"]:
            ev.add("dtstart", dt.date.fromisoformat(e["start"][:10]))
            ev.add("dtend", dt.date.fromisoformat(e["end"][:10]))
        else:
            ev.add("dtstart", dt.datetime.fromisoformat(e["start"]))
            ev.add("dtend", dt.datetime.fromisoformat(e["end"]))
        if e.get("location"):
            ev.add("location", e["location"])
        ev.add("url", e["link"])
        ev.add("description", "\n".join(x for x in [e["link"], e.get("info")] if x))
        ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
        cal.add_component(ev)
    (OUT / "calendar.ics").write_bytes(cal.to_ical())


if __name__ == "__main__":
    main()

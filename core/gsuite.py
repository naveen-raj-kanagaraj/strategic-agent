"""The owner's Gmail (read only) and Google Calendar (read + add events), through Google's official APIs.

Setup once on your Mac:  python scripts/google_login.py  (needs a "Desktop app" OAuth client JSON from Google Cloud).
It writes data/google_token.json (client id, secret and a refresh token; private, never committed). Copy it to the
server with:  bash scripts/server.sh google
Scopes: gmail.readonly (can't send, change or delete mail) and calendar.events (read and add events).
"""
import json
import os
import time
from datetime import datetime, timedelta

import requests

TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "google_token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.events"]
_cache = {"access": "", "exp": 0}


class NotConnected(Exception):
    pass


def connected() -> bool:
    return os.path.isfile(TOKEN_FILE)


def _access() -> str:
    if _cache["access"] and time.time() < _cache["exp"] - 60:
        return _cache["access"]
    if not connected():
        raise NotConnected("Google isn't connected yet. On your Mac run: python scripts/google_login.py, "
                           "then: bash scripts/server.sh google")
    t = json.load(open(TOKEN_FILE))
    r = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "client_id": t["client_id"], "client_secret": t["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise NotConnected(f"Google sign-in expired or was revoked ({r.status_code}). Run scripts/google_login.py again.")
    j = r.json()
    _cache.update(access=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return _cache["access"]


def _get(url, **params):
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {_access()}"}, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------- Gmail (read only) ----------

def emails(query: str = "", hours: int = 24, limit: int = 15) -> list:
    """Recent emails: sender, subject, snippet, time. Promotions and social updates are left out unless asked."""
    q = f"newer_than:{max(1, hours // 24)}d" if hours >= 24 else f"newer_than:{max(1, hours)}h"
    q += " " + (query or "-category:promotions -category:social -category:forums")
    ids = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages", q=q, maxResults=limit).get("messages") or []
    out = []
    for m in ids:
        j = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}", format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
        h = {x["name"].lower(): x["value"] for x in (j.get("payload") or {}).get("headers") or []}
        out.append({"from": h.get("from", ""), "subject": h.get("subject", "(no subject)"), "snippet": j.get("snippet", ""),
                    "ts": int(j.get("internalDate", 0)) // 1000, "important": "IMPORTANT" in (j.get("labelIds") or []),
                    "unread": "UNREAD" in (j.get("labelIds") or [])})
    return out


def email_text(query: str = "", hours: int = 24) -> str:
    try:
        es = emails(query, hours)
    except NotConnected as e:
        return str(e)
    if not es:
        return f"No emails matching that in the last {hours} hours."
    es.sort(key=lambda e: (not e["important"], not e["unread"], -e["ts"]))
    return "\n\n".join(f"{'[important] ' if e['important'] else ''}{'[unread] ' if e['unread'] else ''}"
                       f"From: {e['from']}\nSubject: {e['subject']}\n{datetime.fromtimestamp(e['ts']):%d %b %I:%M %p}\n{e['snippet']}"
                       for e in es)


# ---------- Calendar ----------

def events(days: int = 1, tz: str = "America/Chicago") -> list:
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    start = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    j = _get("https://www.googleapis.com/calendar/v3/calendars/primary/events", singleEvents="true", orderBy="startTime",
             timeMin=start.isoformat(), timeMax=(start + timedelta(days=days)).isoformat(), maxResults=50, timeZone=tz)
    return [{"title": e.get("summary", "(no title)"), "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
             "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"), "where": e.get("location", "")}
            for e in j.get("items") or []]


def events_text(days: int = 1, tz: str = "America/Chicago") -> str:
    try:
        ev = events(days, tz)
    except NotConnected as e:
        return str(e)
    if not ev:
        return "Nothing on your calendar." if days == 1 else f"Nothing on your calendar in the next {days} days."
    def t(x):
        return datetime.fromisoformat(x).strftime("%a %d %b %I:%M %p") if "T" in (x or "") else f"{x} (all day)"
    return "\n".join(f"{t(e['start'])}: {e['title']}{' @ ' + e['where'] if e['where'] else ''}" for e in ev)


def add_event(title: str, start: str, end: str = "", location: str = "", details: str = "", tz: str = "America/Chicago",
              confirmed: bool = False) -> str:
    """Adds to the primary calendar only after the user has confirmed (confirmed=True)."""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end) if end else s + timedelta(hours=1)
    summary = f"{title}, {s:%a %d %b %I:%M %p} to {e:%I:%M %p}{', ' + location if location else ''}"
    if not confirmed:
        return f"NOT ADDED YET. Ask the user to confirm this exact event, then call again with confirmed=true: {summary}"
    try:
        r = requests.post("https://www.googleapis.com/calendar/v3/calendars/primary/events", timeout=20,
                          headers={"Authorization": f"Bearer {_access()}"},
                          json={"summary": title, "location": location, "description": details,
                                "start": {"dateTime": s.isoformat(), "timeZone": tz}, "end": {"dateTime": e.isoformat(), "timeZone": tz}})
    except NotConnected as x:
        return str(x)
    if r.status_code >= 400:
        return f"Google Calendar refused it: {r.status_code} {r.text[:150]}"
    return f"Added to your Google Calendar: {summary}. Link: {r.json().get('htmlLink', '')}"


def day_brief(tz: str = "America/Chicago") -> str:
    """Plain text for the morning brief: today's calendar and important unread mail. '' if not connected."""
    if not connected():
        return ""
    parts = []
    try:
        parts.append("TODAY\n" + events_text(1, tz))
    except Exception as e:
        parts.append(f"TODAY\nCalendar unavailable: {str(e)[:80]}")
    try:
        es = [e for e in emails("is:unread -category:promotions -category:social -category:forums", 24, 20)]
        es.sort(key=lambda e: (not e["important"], -e["ts"]))
        lines = [f"{e['from'].split('<')[0].strip() or e['from']}: {e['subject']}" for e in es[:8]]
        parts.append("EMAIL, UNREAD (last 24 h)\n" + ("\n".join(lines) if lines else "Nothing important unread."))
    except Exception as e:
        parts.append(f"EMAIL\nGmail unavailable: {str(e)[:80]}")
    return "\n\n".join(parts)

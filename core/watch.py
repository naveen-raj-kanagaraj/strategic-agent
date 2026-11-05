"""Instant keyword alerts.

Every ALERT_EVERY_MIN minutes, each watched keyword is checked against the free sources
(Google News, Reddit, Hacker News, your RSS feeds). Anything new since the last check is
sent straight to the user, with no AI call, so alerts cost nothing. The first check of a new
watch only records what already exists, so you don't get a flood of old posts.
"""
import hashlib
import os
from datetime import datetime, timezone

from core import sources, store

ALERT_EVERY_MIN = int(os.getenv("ALERT_EVERY_MIN", "10"))
ALERT_MAX_AGE_HOURS = int(os.getenv("ALERT_MAX_AGE_HOURS", "6"))
MAX_WATCHES_OWNER = 20
MAX_WATCHES_PUBLIC = int(os.getenv("MAX_WATCHES_PUBLIC", "3"))
MAX_ITEMS_PER_ALERT = 5


def _key(item: dict) -> str:
    base = item.get("url") or item.get("title", "")
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def limit_for(uid: str) -> int:
    return MAX_WATCHES_OWNER if uid == "owner" else MAX_WATCHES_PUBLIC


def add(uid: str, chat_id: str, keyword: str) -> str:
    keyword = keyword.strip().strip('"')
    if not keyword:
        return "Tell me the keyword to watch."
    existing = store.list_watches(uid)
    if any(w["keyword"].lower() == keyword.lower() for w in existing):
        return f"Already watching '{keyword}'."
    if len(existing) >= limit_for(uid):
        return f"You can watch up to {limit_for(uid)} keywords. Remove one with /unalert first."
    if not chat_id:
        return "Alerts are sent on Telegram. Message me there once first."
    store.add_watch(uid, chat_id, keyword)
    return (f"Watching '{keyword}'. I'll check news sites, Reddit and Hacker News every {ALERT_EVERY_MIN} minutes "
            "and message you as soon as something new appears.")


def list_text(uid: str) -> str:
    ws = store.list_watches(uid)
    if not ws:
        return "You're not watching any keywords. Add one: /alert Zepto"
    return "Watching:\n" + "\n".join(f"[{w['id']}] {w['keyword']}" for w in ws) + "\n\nRemove one: /unalert <id or keyword>"


def remove(uid: str, what: str) -> str:
    n = store.remove_watch(uid, what)
    return "Stopped watching it." if n else "No watch matched that. See /alerts."


def _age(item):
    if not item.get("published"):
        return ""
    mins = int((datetime.now(timezone.utc) - item["published"]).total_seconds() // 60)
    return f"{mins // 60}h ago" if mins >= 60 else f"{max(mins, 1)}m ago"


def check_watch(w: dict) -> list:
    """New items for one watch since the last check. Seeds silently on the first run."""
    u = store.get_user(w["uid"]) or {}
    from core.news import _region
    items = sources.gather(w["keyword"], [], _region(u.get("timezone")), limit=12, free_only=True)
    fresh = []
    now = datetime.now(timezone.utc)
    for it in items:
        if it.get("published") and (now - it["published"]).total_seconds() > ALERT_MAX_AGE_HOURS * 3600:
            continue
        fresh.append(it)
    new = [it for it in fresh if not store.seen_before(w["uid"], _key(it))]
    store.mark_seen(w["uid"], [_key(it) for it in items])
    if not w["seeded"]:
        store.mark_watch_seeded(w["id"])
        return []
    return new[:MAX_ITEMS_PER_ALERT]


def format_alert(keyword: str, items: list) -> str:
    lines = [f"Alert: '{keyword}'"]
    for it in items:
        where = it["source"] if it["platform"] in ("News", "Web") else f"{it['platform']} {it['source']}".strip()
        age = _age(it)
        lines.append(f"\n{it['title']}\n{where}{', ' + age if age else ''}\n{it['url']}")
    return "\n".join(lines)


def run_watches(send):
    """Called by the scheduler. `send(text, chat_id)` delivers a Telegram message."""
    for w in store.list_watches():
        u = store.get_user(w["uid"]) or {}
        if u.get("blocked"):
            continue
        try:
            new = check_watch(w)
        except Exception as e:
            print(f"Watch {w['id']} failed: {e}")
            continue
        if new:
            send(format_alert(w["keyword"], new), w["chat_id"])

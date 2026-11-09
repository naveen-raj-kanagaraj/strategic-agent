"""Trend alerts for the owner, with a voice note: a story is trending when, within the last few hours,
  - it is on Google's trending searches (India or US) and several news/channel posts are about it, or
  - at least TREND_MIN_ITEMS posts from TREND_MIN_SOURCES different outlets/channels cover the same thing.
Inputs are free: Google Trends' public feed, Google News for the owner's topics, and the owner's Telegram channels
(which also carry what's moving on X and Instagram). Each trend is alerted once.
"""
import os
import re
import time
from collections import Counter

import requests

from core import store

TREND_EVERY_MIN = int(os.getenv("TREND_EVERY_MIN", "20"))
TREND_MIN_ITEMS = int(os.getenv("TREND_MIN_ITEMS", "4"))
TREND_MIN_SOURCES = int(os.getenv("TREND_MIN_SOURCES", "3"))
GEOS = [g.strip() for g in os.getenv("TREND_GEOS", "IN,US").split(",") if g.strip()]
STOP = set("""the a an and or of to in on for with at by from as is are was were be been this that these those it its
into over after before about amid says said new news live update updates today latest how why what who will can
more than just after video watch report reports according first last year years day week time india indian us
world global top big says amid""".split())


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9']{3,}", (text or "").lower()) if w not in STOP}


def google_trending(geo: str) -> list:
    try:
        r = requests.get(f"https://trends.google.com/trending/rss?geo={geo}", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        return [t.strip() for t in re.findall(r"<item>\s*<title>([^<]+)</title>", r.text)][:20]
    except Exception:
        return []


def find(hours: float = 3) -> list:
    """[{title, sources, count, terms, search}] newest-first trends not alerted before."""
    import digest
    items = [it for _, it in digest.owner_items(hours)]
    docs = [(it, _words(f"{it.get('title', '')} {it.get('snippet', '')[:200]}")) for it in items if it.get("title")]
    seen = set(store.kv_get("trend_seen") or [])
    out, used = [], set()
    # 1. Google's trending searches that the news is also covering
    for geo in GEOS:
        for q in google_trending(geo):
            qw = _words(q)
            if not qw:
                continue
            hits = [it for it, w in docs if qw <= w]
            key = "q:" + " ".join(sorted(qw))
            if len(hits) >= 2 and key not in seen:
                out.append({"title": hits[0]["title"], "search": q, "geo": geo, "count": len(hits),
                            "sources": sorted({h.get("source") or "News" for h in hits})[:5], "key": key})
                used |= {id(h) for h in hits}
    # 2. The same story across many outlets/channels
    df = Counter(w for _, ws in docs for w in ws)
    for i, (it, w) in enumerate(docs):
        if id(it) in used:
            continue
        core = {x for x in w if 2 <= df[x] <= len(docs) * 0.3}
        if len(core) < 2:
            continue
        group = [(j, o) for j, (o, ow) in enumerate(docs) if len(core & ow) >= 2]
        srcs = {o.get("source") or "News" for _, o in group}
        key = "c:" + " ".join(sorted(core, key=lambda x: -df[x])[:3])
        if len(group) >= TREND_MIN_ITEMS and len(srcs) >= TREND_MIN_SOURCES and key not in seen:
            out.append({"title": it["title"], "search": "", "geo": "", "count": len(group), "sources": sorted(srcs)[:5], "key": key})
            used |= {id(o) for _, o in group}
    return out[:3]


def run(send_text, send_voice):
    """Scheduler job: alert new trends to the owner (text + voice note). Quiet at night."""
    from datetime import datetime
    hour = datetime.now(store.user_tz({"timezone": store.OWNER_TZ})).hour
    q = os.getenv("OWNER_QUIET", "").strip()
    if re.fullmatch(r"\d{1,2}-\d{1,2}", q):
        a, b = (int(x) for x in q.split("-"))
        if hour >= a or hour < b:
            return
    trends = find()
    if not trends:
        return
    seen = list(store.kv_get("trend_seen") or [])
    for t in trends:
        where = f"trending on Google {t['geo']} as \"{t['search']}\", " if t["search"] else ""
        text = f"TRENDING NOW\n{t['title']}\n{where}{t['count']} reports in the last 3 hours: {', '.join(t['sources'])}"
        send_text(text)
        send_voice(f"Trending now. {t['title']}. {t['count']} reports in the last three hours"
                   + (f", and it's trending on Google {'India' if t['geo'] == 'IN' else t['geo']}." if t["search"] else "."))
        seen.append(t["key"])
    store.kv_set("trend_seen", seen[-500:])
    print(f"Trends sent: {len(trends)}")

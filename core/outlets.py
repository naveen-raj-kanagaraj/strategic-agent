"""Breaking news from the world's main newsrooms, checked every few minutes (free RSS, no AI, no Serper).

Every OUTLETS_EVERY_MIN minutes the front-page feeds of ~25 major outlets are read. A story counts as breaking when
at least OUTLETS_MIN distinct outlets publish it within the last 2 hours: one outlet can run anything, several
newsrooms moving on the same thing at once is the signal. Each story is alerted once, with a voice note.
Extra feeds: OUTLETS_EXTRA=url1,url2 in .env.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

import requests

from core import store

EVERY_MIN = int(os.getenv("OUTLETS_EVERY_MIN", "5"))
MIN_OUTLETS = int(os.getenv("OUTLETS_MIN", "3"))
WINDOW_H = 2

OUTLETS = {
    "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC Tech": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "NYT": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "NYT Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "WSJ": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "WSJ Markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "CNBC Tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "NPR": "https://feeds.npr.org/1001/rss.xml",
    "CBS News": "https://www.cbsnews.com/latest/rss/main",
    "ABC News": "https://abcnews.go.com/abcnews/topstories",
    "Fox News": "https://moxie.foxnews.com/google-publisher/latest.xml",
    "Politico": "https://rss.politico.com/politics-news.xml",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "NDTV": "https://feeds.feedburner.com/ndtvnews-top-stories",
    "The Hindu": "https://www.thehindu.com/news/national/feeder/default.rss",
    "Times of India": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "Indian Express": "https://indianexpress.com/feed/",
    "Economic Times": "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/latestnews.xml",
    "Hindustan Times": "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml",
}
for i, u in enumerate(x.strip() for x in os.getenv("OUTLETS_EXTRA", "").split(",") if x.strip()):
    OUTLETS[f"Extra {i + 1}"] = u

STOP = set("""the a an and or of to in on for with at by from as is are was were be been this that these those it its
into over after before about amid says said new news live update updates today latest how why what who will can
more than just video watch report reports first last year years day week time world after has have had not but his
her their they you your our out up down off amid why when where which while""".split())
UA = {"User-Agent": "Mozilla/5.0 (newsreader)"}


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9']{3,}", (t or "").lower()) if w not in STOP}


def _read(name_url):
    name, url = name_url
    try:
        r = requests.get(url, headers=UA, timeout=12)
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    for it in list(root.iter("item"))[:40]:
        title = (it.findtext("title") or "").strip()
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        try:
            ts = parsedate_to_datetime(pub).timestamp() if "," in pub else time.time()
        except Exception:
            ts = time.time()
        if title and time.time() - ts < WINDOW_H * 3600:
            out.append({"outlet": name, "title": title, "link": (it.findtext("link") or "").strip(), "ts": ts})
    return out


def scan() -> list:
    with ThreadPoolExecutor(8) as ex:
        items = [x for batch in ex.map(_read, OUTLETS.items()) for x in batch]
    docs = [(it, _words(it["title"])) for it in items]
    df = Counter(w for _, ws in docs for w in ws)
    seen, out, used = set(store.kv_get("outlets_seen") or []), [], set()
    for i, (it, w) in enumerate(sorted(docs, key=lambda d: d[0]["ts"])):
        if id(it) in used:
            continue
        core = {x for x in w if 2 <= df[x] <= max(3, len(docs) * .15)}
        if len(core) < 2:
            continue
        group = [o for o, ow in docs if len(core & ow) >= 2]
        outlets = sorted({o["outlet"].split(" ")[0] for o in group})
        if len(outlets) < MIN_OUTLETS:
            continue
        key = " ".join(sorted(core, key=lambda x: -df[x])[:4])
        if any(len(set(key.split()) & set(k.split())) >= 3 for k in seen):
            used |= {id(o) for o in group}
            continue
        first = min(group, key=lambda o: o["ts"])
        out.append({"title": first["title"], "outlets": outlets, "count": len(group), "link": first["link"], "key": key})
        used |= {id(o) for o in group}
    return out[:3]


def run(send_text, send_voice):
    stories = scan()
    if not stories:
        return
    seen = list(store.kv_get("outlets_seen") or [])
    for s in stories:
        send_text(f"BREAKING\n{s['title']}\nReported by {len(s['outlets'])} outlets in the last 2 hours: "
                  f"{', '.join(s['outlets'][:6])}\n{s['link']}")
        send_voice(f"Breaking. {s['title']}. Reported by {len(s['outlets'])} major outlets in the last two hours.")
        seen.append(s["key"])
    store.kv_set("outlets_seen", seen[-800:])
    print(f"Breaking alerts sent: {len(stories)}")

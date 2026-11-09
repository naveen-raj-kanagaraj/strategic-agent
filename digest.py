import os
import requests
import schedule
import time
import threading
from datetime import datetime
from core.tools import web_search
from core import store
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from dotenv import load_dotenv
from core import llm
import re
import discord
import asyncio
import json

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_USER_ID = (os.getenv("ALLOWED_DISCORD_IDS") or "").split(",")[0]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")

# Where briefs go: telegram, discord, or both
DIGEST_TO = os.getenv("DIGEST_TO", "telegram").lower()
# Local times for the two daily briefs, and minutes between breaking-news checks
DIGEST_TIMES = [t.strip() for t in os.getenv("DIGEST_TIMES", "08:00,19:00").split(",") if t.strip()]
BREAKING_EVERY_MIN = int(os.getenv("BREAKING_EVERY_MIN", "20"))
client = llm.Client("digest")

NEWS_TIMES = [t.strip() for t in os.getenv("NEWS_TIMES", "08:00,19:00").split(",") if t.strip()]
RADAR_TIME = os.getenv("RADAR_TIME", "10:00")  # owner's local time; empty disables
NEWS_WINDOW_MIN = int(os.getenv("NEWS_CATCHUP_MIN", "180"))  # if the bot was down at 8:00, still send until 11:00
OFFLINE_ALERT_MIN = 10


def report_downtime(now: float = None):
    """On start-up, tell the owner how long the bot was offline (Mac asleep, Wi-Fi down, crash)."""
    now = now or time.time()
    last = store.kv_get("scheduler_tick")
    if not last or now - last < OFFLINE_ALERT_MIN * 60:
        return None
    mins = int((now - last) // 60)
    tz = store.user_tz({"timezone": store.OWNER_TZ})
    frm = datetime.fromtimestamp(last, tz).strftime("%a %I:%M %p")
    to = datetime.fromtimestamp(now, tz).strftime("%I:%M %p")
    dur = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} min"
    log = (store.kv_get("downtime_log", []) + [{"from": last, "to": now}])[-20:]
    store.kv_set("downtime_log", log)
    text = (f"I was offline for {dur} ({frm} to {to}). Reminders that came due are being sent now, and news "
            f"briefs missed in the last {NEWS_WINDOW_MIN // 60}h will catch up. Live updates and messages sent to me "
            "while I was down were not answered.")
    from core import alerts
    alerts.send_owner(text)
    return text


def load_covered_news() -> list:
    return store.kv_get("covered_news", [])


def save_covered_news(news: list):
    store.kv_set("covered_news", news[-100:])


def send_discord(message: str):
    try:
        async def _send():
            intents = discord.Intents.default()
            dc = discord.Client(intents=intents)

            @dc.event
            async def on_ready():
                user = await dc.fetch_user(int(DISCORD_USER_ID))
                dm = await user.create_dm()
                chunks = [message[i:i+2000] for i in range(0, len(message), 2000)]
                for chunk in chunks:
                    await dm.send(chunk)
                    await asyncio.sleep(0.5)
                await dc.close()

            await dc.start(DISCORD_BOT_TOKEN)

        asyncio.run(_send())
    except Exception as e:
        print(f"Discord send error: {e}")


def send_telegram(message: str, chat_id: str = None, html: bool = False, markup: dict = None) -> bool:
    """Send via the Bot API. Returns False if the chat can't be reached (e.g. user blocked the bot)."""
    chat_id = chat_id or OWNER_TELEGRAM_ID
    if not (TELEGRAM_BOT_TOKEN and chat_id):
        print("Telegram send skipped: TELEGRAM_BOT_TOKEN or chat id missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok = True
    for i in range(0, len(message), 4096):
        try:
            body = {"chat_id": chat_id, "text": message[i:i + 4096], "disable_web_page_preview": True}
            if html:
                body["parse_mode"] = "HTML"
            if markup and i + 4096 >= len(message):  # buttons go under the last part
                body["reply_markup"] = markup
            r = requests.post(url, json=body, timeout=20)
            if not r.ok:
                print(f"Telegram send error to {chat_id}: {r.status_code} {r.text[:200]}")
                if r.status_code in (400, 403):
                    return False
                ok = False
        except Exception as e:
            print(f"Telegram send error: {e}")
            ok = False
        time.sleep(0.5)
    return ok


def deliver(message: str, markup: dict = None):
    if DIGEST_TO in ("telegram", "both"):
        send_telegram(message, markup=markup)
    if DIGEST_TO in ("discord", "both") and DISCORD_BOT_TOKEN:
        send_discord(message)


def search_clean(query: str) -> str:
    results = web_search(query)
    lines = []
    for line in results.split("\n"):
        if not line.startswith("URL:") and not line.startswith("http"):
            lines.append(line)
    return "\n".join(lines)[:600]


# ---------- owner news feeds ----------

# Short feed queries (Google News search, newest first). Each category can have several.
OWNER_FEEDS = {
    "World": ["world news", "breaking news"],
    "India": ["India", "India breaking news"],
    "Tamil Nadu": ["Tamil Nadu", "Chennai"],
    "Gaming and esports": ["esports", "gaming industry", "BGMI"],
    "Gaming deals": ["gaming acquisition", "game studio funding"],
    "Geopolitics": ["geopolitics", "sanctions"],
    "Markets": ["stock market", "Sensex Nifty", "Wall Street"],
    "Crypto": ["bitcoin", "crypto market"],
    "Creators and marketing": ["creator economy", "influencer marketing", "YouTube creators"],
    "Tech and AI": ["artificial intelligence", "OpenAI Anthropic Google AI", "Nvidia Jensen Huang",
                    "Apple Microsoft Google Meta Amazon Tesla"],
    "Economy": ["inflation interest rates", "RBI Federal Reserve"],
}
LATEST_QUIET = (23, 7)          # no hourly updates 11 PM - 7 AM (owner's time); they roll into the morning
LATEST_MAX_ITEMS = 12


def _owner_feeds() -> dict:
    feeds = dict(OWNER_FEEDS)
    owner = store.get_user("owner") or {}
    known = " ".join(feeds).lower()
    for topic in store.interests(owner)[:4]:
        if topic.lower() not in known:
            feeds[topic] = [topic]
    return feeds


def owner_items(max_age_hours: float, include_channels: bool = True) -> list:
    """[(category, item)] from Google News (plus Hacker News for tech), newer than max_age_hours, newest first."""
    from core import sources
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    out = []
    for cat, queries in _owner_feeds().items():
        region = "in" if cat in ("India", "Tamil Nadu", "Markets") else "us"
        items = []
        for q in queries:
            items += sources.google_news(q, region)
        if cat == "Tech and AI":
            items += sources.hacker_news("AI")
            items += sources.google_headlines("TECHNOLOGY", "us")
        if cat == "World":            # the biggest stories right now, whatever the topic
            items += sources.google_headlines("", "us") + sources.google_headlines("WORLD", "us")
        if cat == "India":
            items += sources.google_headlines("", "in")
        if cat == "Markets":
            items += sources.google_headlines("BUSINESS", "us") + sources.google_headlines("BUSINESS", "in")
        for it in items:
            if it.get("published") and (now - it["published"]).total_seconds() <= max_age_hours * 3600:
                out.append((cat, it))
    if include_channels:  # posts from your joined channels and the public ones you follow
        try:
            from core import channels
            since = time.time() - max_age_hours * 3600
            out += [(f"Telegram: {i['source']}", i) for i in channels.posts_for("owner", since)]
        except Exception as e:
            print(f"Channel posts unavailable: {e}")
    floor = datetime.min.replace(tzinfo=_tz.utc)
    out.sort(key=lambda ci: ci[1].get("published") or floor, reverse=True)
    return out


def _ago(item) -> str:
    from datetime import timezone as _tz
    mins = int((datetime.now(_tz.utc) - item["published"]).total_seconds() // 60)
    return f"{mins // 60}h {mins % 60}m ago" if mins >= 60 else f"{max(mins, 1)}m ago"


def build_latest(window_min: int = None) -> str:
    """Hourly: only stories published since the last check that were never sent before. '' if nothing new."""
    from core import seen
    window = window_min or (BREAKING_EVERY_MIN + 30)
    pairs = owner_items(window / 60)
    fresh = seen.filter_new(seen.scopes_for("owner"), [it for _, it in pairs])
    if not fresh:
        return ""
    keep = {id(i) for i in fresh}
    pairs = [(c, i) for c, i in pairs if id(i) in keep]
    # at most 3 per category so one busy topic can't fill the message; still newest first overall
    per, chosen = {}, []
    for c, i in pairs:
        if per.get(c, 0) < 3:
            chosen.append((c, i))
            per[c] = per.get(c, 0) + 1
        if len(chosen) >= LATEST_MAX_ITEMS:
            break
    seen.remember("owner|latest", [i for _, i in chosen])
    from core import newslinks
    newslinks.save("owner", [i for _, i in chosen])
    now = datetime.now(store.user_tz({"timezone": store.OWNER_TZ}))
    lines = [f"Latest, {now:%I:%M %p} (newest first)"]
    for c, i in chosen:
        lines.append(f"\n[{c}] {i['title']}\n{i['source'] or 'News'}, {_ago(i)}")
    return "\n".join(lines)


def build_digest():
    now = datetime.now().strftime("%A, %B %d %Y, %I:%M %p")
    from core import seen
    categories = _owner_feeds()
    pairs = owner_items(14)  # since roughly the previous briefing
    fresh = seen.filter_new("owner|brief", [i for _, i in pairs])
    keep = {id(i) for i in fresh}
    by_cat = {}
    for c, i in pairs:
        if id(i) in keep and len(by_cat.setdefault(c, [])) < 6:
            by_cat[c].append(i)
    tg = [i for c, i in pairs if c.startswith("Telegram: ") and id(i) in keep][:15]
    for c in [c for c in by_cat if c.startswith("Telegram: ")]:
        by_cat.pop(c)
    if tg:
        categories = {**categories, "Your Telegram channels": []}
        by_cat["Your Telegram channels"] = tg
    used = [i for items in by_cat.values() for i in items]
    combined = ""
    for c in categories:
        items = by_cat.get(c, [])
        combined += f"\n{c}:\n" + ("\n".join(f"- ({_ago(i)}) {i['title']} [{i['source']}]: {i['snippet'][:200]}"
                                               for i in items) if items else "- nothing new since the last briefing") + "\n"
    covered_context = ("Every story below is new: none appeared in an earlier briefing. Use only these. If a topic says "
                       "'nothing new', write one line saying so instead of padding it.")

    response = client.messages.create(
        _uid="owner",
        model="claude-haiku-4-5-20251001",
        max_tokens=8000,                 # 4000 was hit on every brief, so they were being cut off
        messages=[{
            "role": "user",
            "content": f"""You are a world-class strategic analyst, historian, and mentor. Write a sharp briefing for Naveen, a marketing analytics student at DePaul University who runs a gaming media brand and is building a talent agency, content house, and production studio.

STRICT RULES:
- Plain text only. Zero markdown. No ## no ** no -- no bullet points.
- No URLs or source names.
- Each topic is independent. Do not connect topics to each other.
- Lead each topic with the newest development (the items show how long ago each was published).
- For each topic: write what happened in detail, then the historical parallel, then the prediction. Then immediately write the SWOT for that topic only.
- Each topic section should be 6-10 sentences of detailed analysis. Never cut off mid-sentence.
- SWOT should be 3-4 sentences per quadrant covering that topic specifically.
- Add exactly two line breaks between each topic so it reads cleanly.
- No strategic take. No today's lesson. Do not include these unless asked.

{covered_context}

TOPICS IN ORDER (treat each as completely independent):
{chr(10).join(categories)}

AFTER ALL TOPICS write only:
Trend Radar: 5 early signals most people are missing right now across any of the topics. One sentence each. Each on its own line.

NEWS DATA:
{combined}

Current time: {now}"""
        }]
    )

    analysis = response.content[0].text
    analysis = re.sub(r'#{1,6}\s*', '', analysis)
    analysis = re.sub(r'\*{1,3}', '', analysis)
    analysis = re.sub(r'-{2,}', '', analysis)
    analysis = re.sub(r'\n{3,}', '\n\n', analysis)

    seen.remember("owner|brief", used)

    final = f"BRIEFING\n{now}\n\n{analysis.strip()}"
    try:            # your day first: calendar and important unread email (when Google is connected)
        from core import gsuite
        day = gsuite.day_brief(store.OWNER_TZ)
        if day:
            final = f"BRIEFING\n{now}\n\n{day}\n\n{analysis.strip()}"
    except Exception as e:
        print(f"Day brief unavailable: {e}")
    return final


def check_and_send_breaking():
    """Hourly 'latest' update for the owner: new stories only, newest first, quiet at night."""
    hour = datetime.now(store.user_tz({"timezone": store.OWNER_TZ})).hour
    if hour >= LATEST_QUIET[0] or hour < LATEST_QUIET[1]:
        return
    text = build_latest()
    if text:
        from core import newslinks
        deliver(text, newslinks.markup(newslinks.last_key("owner")))
        print(f"Latest update sent ({text.count(chr(10) + '[')} stories)")


def send_digest():
    print(f"Sending digest at {datetime.now().strftime('%H:%M')}")
    try:
        digest = build_digest()
        deliver(digest)
        print(f"Digest sent to {DIGEST_TO}.")
    except Exception as e:
        print(f"Digest error: {e}")
        from core import alerts
        alerts.notify("digest", f"Your scheduled briefing failed: {str(e)[:200]}", 120)


def _next_repeat(due_utc: str, repeat: str, tzname: str):
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tzname or store.OWNER_TZ)
    except Exception:
        tz = ZoneInfo(store.OWNER_TZ)
    due = datetime.fromisoformat(due_utc.replace("Z", "+00:00")).astimezone(tz)
    now = datetime.now(tz)
    step = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "weekdays": timedelta(days=1)}.get(repeat)
    if not step:
        return None
    while due <= now or (repeat == "weekdays" and due.weekday() >= 5):
        due += step
    return store.iso(due)


def send_due_reminders():
    for r in store.due_reminders():
        u = store.get_user(r["uid"]) or {}
        ok = send_telegram(f"Reminder: {r['text']}", r["chat_id"])
        nxt = _next_repeat(r["due_utc"], r["repeat"], u.get("timezone")) if ok or r["repeat"] else None
        store.finish_reminder(r["id"], nxt)


def _send_user_news(u: dict, key: str):
    from core.news import build_user_brief
    store.update_user(u["uid"], last_news_key=key)  # mark first so a slow build never double-sends
    try:
        brief = build_user_brief(u["uid"])
    except Exception as e:
        print(f"News for {u['uid']} failed: {e}")
        return
    if brief.startswith("Nothing new"):
        return  # scheduled briefs stay quiet when there's nothing new
    from core import newslinks
    if not send_telegram(brief, u["chat_id"], markup=newslinks.markup(newslinks.last_key(u["uid"]))):
        store.update_user(u["uid"], news_on=0)  # they blocked the bot or the chat is gone
        print(f"News off for {u['uid']}: chat unreachable")


_pool = ThreadPoolExecutor(max_workers=3)


def send_user_news():
    for u in store.all_users():
        if not (u.get("news_on") and u.get("state") == "active" and u.get("chat_id")) or u.get("blocked"):
            continue
        if not store.interests(u):
            continue
        now = datetime.now(store.user_tz(u))
        from core.topics import slots_for
        for slot in slots_for(u.get("news_mode") or "both", NEWS_TIMES):
            try:
                hh, mm = map(int, slot.split(":"))
            except ValueError:
                continue
            start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            key = f"{now:%Y-%m-%d}-{slot}"
            if start <= now < start + timedelta(minutes=NEWS_WINDOW_MIN) and u.get("last_news_key") != key:
                _pool.submit(_send_user_news, u, key)


def send_radar():
    from core.radar import build_radar
    try:
        send_telegram(build_radar())
    except Exception as e:
        print(f"Radar error: {e}")
        from core import alerts
        alerts.notify("radar", f"The daily radar failed: {str(e)[:200]}", 360)


def start_digest_scheduler():
    try:
        report_downtime()
    except Exception as e:
        print(f"Downtime report failed: {e}")
    for t in DIGEST_TIMES:
        schedule.every().day.at(t).do(send_digest)
    if BREAKING_EVERY_MIN > 0:
        schedule.every(BREAKING_EVERY_MIN).minutes.do(check_and_send_breaking)
    if RADAR_TIME:
        schedule.every().day.at(RADAR_TIME).do(send_radar)
    print(f"Scheduler: owner briefs at {', '.join(DIGEST_TIMES)} to {DIGEST_TO}; breaking every {BREAKING_EVERY_MIN} min; "
          f"radar at {RADAR_TIME or 'off'}; user news at {', '.join(NEWS_TIMES)} local; reminders every 30 s")
    from core import alerts, talent, watch
    if watch.ALERT_EVERY_MIN > 0:
        schedule.every(watch.ALERT_EVERY_MIN).minutes.do(watch.run_watches, send_telegram)
    from core import channels
    if channels.CHANNEL_EVERY_MIN > 0:
        schedule.every(channels.CHANNEL_EVERY_MIN).minutes.do(lambda: _pool.submit(channels.sync))
        _pool.submit(channels.sync)  # read channels once at start-up
    schedule.every().day.at("03:40").do(channels.prune)
    from core import topics
    if topics.LIVE_EVERY_MIN > 0:
        schedule.every(topics.LIVE_EVERY_MIN).minutes.do(
            lambda: _pool.submit(topics.run_live,
                                 lambda text, cid, markup=None: send_telegram(text, cid, html=True, markup=markup)))
    schedule.every().monday.at(talent.WEEKLY_TIME).do(talent.send_weekly, send_telegram)
    from core import backup
    schedule.every().day.at(os.getenv("BACKUP_TIME", "03:00")).do(backup.nightly)
    def _update_ytdlp():      # X, Instagram and TikTok change often; an old yt-dlp is the usual reason downloads break
        import subprocess, sys
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], capture_output=True, text=True, timeout=300)
        print(f"yt-dlp update: {'ok' if r.returncode == 0 else r.stderr[-200:]}")
    schedule.every().day.at("04:10").do(lambda: _pool.submit(_update_ytdlp))
    _pool.submit(_update_ytdlp)
    from core import tasks as bg
    schedule.every(10).minutes.do(lambda: _pool.submit(bg.run_due, lambda t, cid: send_telegram(t, cid)))
    if bg.LOOSE_ENDS_TIME:
        schedule.every().day.at(bg.LOOSE_ENDS_TIME).do(lambda: _pool.submit(bg.loose_ends, lambda t, cid=None: send_telegram(t)))
    from core import speak, trends
    if trends.TREND_EVERY_MIN > 0:
        schedule.every(trends.TREND_EVERY_MIN).minutes.do(lambda: _pool.submit(
            trends.run, lambda t: send_telegram(t), lambda t: speak.send_voice(t, OWNER_TELEGRAM_ID)))
    schedule.every().day.at("03:30").do(store.prune_seen)
    schedule.every().day.at("03:35").do(store.prune_activity)
    while True:
        store.kv_set("scheduler_tick", time.time())
        for job in (schedule.run_pending, send_due_reminders, send_user_news):
            try:
                job()
            except Exception as e:
                name = getattr(job, "__name__", str(job))
                print(f"Scheduler error in {name}: {e}")
                alerts.notify(f"sched:{name}", f"Scheduled job '{name}' failed: {str(e)[:200]}", 120)
        time.sleep(30)


def run_digest():
    t = threading.Thread(target=start_digest_scheduler, daemon=True)
    t.start()
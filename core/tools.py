"""Tools the agent can call. Every tool acts for the current user (core.context)."""
import os
import tempfile
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from core import context, store
from core.downloader import tool_download_video
from core.web import read_url
from core import sources, talent, watch

PUBLIC_IMAGE_GEN = os.getenv("PUBLIC_IMAGE_GEN", "false").lower() == "true"


def _uid() -> str:
    return context.get().uid or "unknown"


def _tz() -> ZoneInfo:
    u = store.get_user(_uid()) or {}
    return store.user_tz(u)


def web_search(query: str) -> str:
    try:
        url = "https://google.serper.dev/search"
        headers = {
            "X-API-KEY": os.getenv("SERPER_API_KEY"),
            "Content-Type": "application/json"
        }
        payload = {"q": query, "num": 6}
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        store.bump("serper")
        data = response.json()
        try:      # Google News, last 24 hours: what happened today, newest first (Google's main results lag)
            news = requests.post("https://google.serper.dev/news", headers=headers,
                                 json={"q": query, "num": 6, "tbs": "qdr:d"}, timeout=15).json().get("news", [])
            store.bump("serper")
        except Exception:
            news = []

        output = f"Searched on {datetime.now(_tz()):%A %d %B %Y, %I:%M %p}. Prefer the newest dated results; they override anything you remember.\n\n"
        if news:
            output += "NEWS, LAST 24 HOURS:\n"
            for r in news[:6]:
                output += f"{r.get('date', '')} | {r.get('source', '')} | {r.get('title', '')}\n{r.get('snippet', '')}\nURL: {r.get('link', '')}\n\n"
            output += "WEB:\n"
        if "answerBox" in data:
            output += f"Answer: {data['answerBox'].get('answer', '') or data['answerBox'].get('snippet', '')}\n\n"
        for r in data.get("organic", [])[:6]:
            output += f"Title: {r.get('title', '')}{' (' + r['date'] + ')' if r.get('date') else ''}\n"
            output += f"Summary: {r.get('snippet', '')}\n"
            output += f"URL: {r.get('link', '')}\n\n"

        return output.strip() if (news or data.get("organic")) else "No results found."
    except Exception as e:
        return f"Search failed: {str(e)}"


def scan_sources(query: str) -> str:
    """Keyword scan across news sites, Reddit, Hacker News, X/Instagram/Threads and YouTube."""
    from core.news import _region, _fmt
    u = store.get_user(_uid()) or {}
    words = [w.strip() for w in query.split(",") if w.strip()]
    items = sources.gather(words[0], words[1:], _region(u.get("timezone")), limit=10)
    if not items:
        return "Nothing matching that in the last day across news, Reddit, HN, X, Instagram, Threads or YouTube."
    return "Matches from the last day (best first):\n" + "\n".join(_fmt(i) for i in items)


def watch_keyword(keyword: str) -> str:
    ctx = context.get()
    u = store.get_user(ctx.uid) or {}
    return watch.add(ctx.uid, u.get("chat_id") or (ctx.chat_id if ctx.platform == "telegram" else ""), keyword)


def _owner_only():
    return None if context.get().is_owner else "Email and calendar are private to the bot owner."


def _talent_only():
    return None if context.get().is_owner else "The talent tracker is private to the bot owner."


def save_note(content: str, tag: str = "general") -> str:
    nid = store.add_note(_uid(), content, tag)
    return f"Note saved. ID: {nid}, tag: '{tag or 'general'}'."


def get_notes(tag: str = None) -> str:
    notes = store.get_notes(_uid(), tag)
    if not notes:
        return f"No notes found under tag '{tag}'." if tag else "No notes saved yet."
    return "\n\n".join(f"[{n['id']}] [{n['tag']}] {n['created_at'][:10]}\n{n['content']}" for n in notes)


def delete_note(note_id: int) -> str:
    return "Note deleted." if store.delete_note(_uid(), int(note_id)) else "No note with that ID."


def remember(fact: str) -> str:
    store.add_fact(_uid(), fact)
    return "Saved to long-term memory."


def forget(about: str) -> str:
    n = store.delete_facts_matching(_uid(), about)
    return f"Forgot {n} saved fact(s)." if n else "Nothing saved matched that."


def get_datetime() -> str:
    tz = _tz()
    return datetime.now(tz).strftime(f"Date: %A, %B %d %Y | Time: %I:%M %p ({tz.key})")


def set_reminder(text: str, when: str, repeat: str = "") -> str:
    """`when` is a local date-time like 2026-10-09T09:00 in the user's timezone."""
    ctx = context.get()
    tz = _tz()
    try:
        local = datetime.fromisoformat(when.strip().replace("Z", ""))
    except ValueError:
        return "Couldn't understand that time. Use format YYYY-MM-DDTHH:MM."
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz)
    if local < datetime.now(tz) - timedelta(minutes=1):
        return "That time is in the past. Ask the user for a future time."
    repeat = (repeat or "").lower().strip()
    if repeat not in ("", "daily", "weekly", "weekdays"):
        repeat = ""
    user = store.get_user(ctx.uid) or {}
    chat_id = user.get("chat_id") or ctx.chat_id
    if not chat_id:
        return "I can only send reminders on Telegram. Ask the user to message me there once."
    rid = store.add_reminder(ctx.uid, chat_id, text, store.iso(local), repeat)
    when_txt = local.strftime("%a %b %d, %I:%M %p")
    return f"Reminder {rid} set for {when_txt} ({tz.key}){' repeating ' + repeat if repeat else ''}."


def list_reminders() -> str:
    tz = _tz()
    items = store.list_reminders(_uid())
    if not items:
        return "No upcoming reminders."
    out = []
    for r in items:
        due = datetime.fromisoformat(r["due_utc"].replace("Z", "+00:00")).astimezone(tz)
        out.append(f"[{r['id']}] {due.strftime('%a %b %d, %I:%M %p')}{' (' + r['repeat'] + ')' if r['repeat'] else ''}: {r['text']}")
    return "\n".join(out)


def cancel_reminder(reminder_id: int) -> str:
    return "Reminder cancelled." if store.cancel_reminder(_uid(), int(reminder_id)) else "No active reminder with that ID."


IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "auto")        # auto | openai | gemini | pollinations
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
_SIZES = {"square": ("1024x1024", "1:1"), "landscape": ("1536x1024", "16:9"), "portrait": ("1024x1536", "9:16")}


def _image_openai(prompt: str, aspect: str) -> bytes:
    import base64
    r = requests.post("https://api.openai.com/v1/images/generations", timeout=180,
                      headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
                      json={"model": OPENAI_IMAGE_MODEL, "prompt": prompt[:4000], "size": _SIZES[aspect][0], "quality": "high"})
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI images {r.status_code}: {r.text[:200]}")
    return base64.b64decode(r.json()["data"][0]["b64_json"])


def _image_gemini(prompt: str, aspect: str) -> bytes:
    import base64
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent",
                      params={"key": os.getenv("GEMINI_API_KEY")}, timeout=180,
                      json={"contents": [{"parts": [{"text": prompt[:4000]}]}],
                            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": _SIZES[aspect][1]}}})
    if r.status_code != 200:
        raise RuntimeError(f"Gemini images {r.status_code}: {r.text[:200]}")
    for part in ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
        data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
        if data:
            return base64.b64decode(data)
    raise RuntimeError("Gemini returned no image (it may have refused the prompt).")


def _image_pollinations(prompt: str, aspect: str) -> bytes:
    w, h = _SIZES[aspect][0].split("x")
    r = requests.get(f"https://image.pollinations.ai/prompt/{quote(prompt[:800])}?width={w}&height={h}&nologo=true&model=flux", timeout=90)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"Pollinations failed ({r.status_code})")
    return r.content


def generate_image(prompt: str, aspect: str = "square") -> str:
    """Best available image model: OpenAI (gpt-image) if OPENAI_API_KEY is set, else Google Gemini image if
    GEMINI_API_KEY is set, else the free Pollinations service (low quality; last resort)."""
    ctx = context.get()
    if not ctx.is_owner and not PUBLIC_IMAGE_GEN:
        return "Image generation is only available to the bot owner."
    if ctx.platform != "telegram":
        return "On this platform, ask with 'draw ...' or 'imagine ...' instead."
    aspect = aspect if aspect in _SIZES else "square"
    order = {"openai": ["openai"], "gemini": ["gemini"], "pollinations": ["pollinations"]}.get(IMAGE_PROVIDER) or \
        ([p for p, k in (("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")) if os.getenv(k)] + ["pollinations"])
    errors, data, used = [], None, ""
    for p in order:
        try:
            data = {"openai": _image_openai, "gemini": _image_gemini, "pollinations": _image_pollinations}[p](prompt, aspect)
            used = p
            break
        except Exception as e:
            errors.append(str(e)[:150])
    if not data:
        return "Image generation failed: " + " | ".join(errors)
    fd, path = tempfile.mkstemp(suffix=".png", prefix="agentimg_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    ctx.outbox.append({"kind": "photo", "path": path, "dir": None, "caption": prompt[:200]})
    weak = " (made with the free fallback: add OPENAI_API_KEY or GEMINI_API_KEY for much better images)" if used == "pollinations" else ""
    return f"Image generated{weak}. It will be sent right after your reply; keep your reply to one short line."


import ast
import math
import operator

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": lambda *a: sum(a),
    "pow": pow, "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
    "exp": math.exp, "ceil": math.ceil, "floor": math.floor,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(e) for e in node.elts]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS and not node.keywords):
        args = []
        for a in node.args:
            v = _eval_node(a)
            args.extend(v if isinstance(v, list) else [v])
        return _FUNCS[node.func.id](*args)
    raise ValueError("only numbers, + - * / // % **, and basic math functions are allowed")


def calculate(expression: str) -> str:
    """Safe calculator: parses the expression as math only, never runs code."""
    try:
        expr = expression.replace("^", "**").replace(",", "") if expression.count(",") and "(" not in expression else expression.replace("^", "**")
        result = _eval_node(ast.parse(expr, mode="eval"))
        if isinstance(result, float):
            result = round(result, 10)
        return f"{expression} = {result}"
    except Exception as e:
        return f"Error: {str(e)}"


def _schema(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


S = {"type": "string"}
I = {"type": "integer"}

ALL_TOOLS = [
    _schema("web_search", "Search Google for current news, prices, facts, people, events. Returns top snippets only; "
            "use read_url on a result to read the full page when details matter.",
            {"query": {**S, "description": "Specific search query."}}, ["query"]),
    _schema("scan_sources", "Find what news sites, Reddit, Hacker News, X, Instagram, Threads and YouTube are saying "
            "in the last day about a topic, matched by keywords and ranked by relevance, recency and engagement. Use for "
            "'what's the buzz about', 'what is Reddit/X saying', trend checks. Pass comma-separated keywords, main one first.",
            {"query": {**S, "description": "e.g. 'Zepto, Blinkit, quick commerce'"}}, ["query"]),
    _schema("read_url", "Open a link and read it: articles, blog posts, PDFs, and YouTube video transcripts. Use this "
            "whenever the user shares a link (other than a short video they want downloaded) or asks about a page.",
            {"url": S}, ["url"]),
    _schema("download_video", "Download a video from an X, Instagram, TikTok, YouTube Shorts, Reddit, Facebook or Threads "
            "link and send the file to the user. Also returns its title and caption.",
            {"url": S}, ["url"]),
    _schema("remember", "Save a durable fact about the user to long-term memory (preferences, projects, people, goals, "
            "deadlines). Use when they say 'remember...' or share something clearly worth keeping.",
            {"fact": S}, ["fact"]),
    _schema("forget", "Delete saved facts about the user that contain the given text.", {"about": S}, ["about"]),
    _schema("set_reminder", "Schedule a Telegram reminder. Convert the user's words to a local date-time in THEIR "
            "timezone (given in the system prompt) using the current time.",
            {"text": {**S, "description": "What to remind them about, written as the reminder message."},
             "when": {**S, "description": "Local date-time, format YYYY-MM-DDTHH:MM"},
             "repeat": {**S, "description": "'', 'daily', 'weekdays' or 'weekly'"}}, ["text", "when"]),
    _schema("list_reminders", "List the user's upcoming reminders with IDs."),
    _schema("cancel_reminder", "Cancel a reminder by ID.", {"reminder_id": I}, ["reminder_id"]),
    _schema("watch_keyword", "Start instant alerts: the user gets a Telegram message within ~30 minutes whenever "
            "news sites, Reddit or Hacker News post something new matching this keyword. Use for 'alert me', "
            "'tell me when', 'keep an eye on'.", {"keyword": S}, ["keyword"]),
    _schema("list_watches", "List the user's keyword alerts."),
    _schema("unwatch_keyword", "Stop alerts for a keyword or watch ID.", {"keyword": S}, ["keyword"]),
    _schema("save_creator", "Talent agency tracker: add or update a creator (matched by name). Use whenever the owner "
            "mentions a creator they're scouting, talking to or managing. Only include fields mentioned.",
            {"name": S, "platform": S, "handle": S, "followers": S, "niche": S,
             "rate": {**S, "description": "e.g. '₹40K per reel'"},
             "contact": S, "status": {**S, "description": "prospect, contacted, in talks, signed, passed"},
             "notes": {**S, "description": "New information to append"}}, ["name"]),
    _schema("find_creators", "Search the owner's creators by name, niche, platform or notes, optionally by status.",
            {"query": S, "status": S}),
    _schema("save_deal", "Talent agency tracker: add or update a brand deal. Stages: lead, pitched, negotiating, "
            "signed, live, paid, lost. Pass deal_id to update a specific deal; otherwise matched by creator + brand. "
            "For follow-ups, also set a reminder.",
            {"creator": S, "brand": S, "deal_id": I, "value": {**S, "description": "e.g. '₹1.2L'"},
             "stage": S, "due": {**S, "description": "YYYY-MM-DD"}, "notes": S}),
    _schema("pipeline", "The owner's talent pipeline: open deals by stage, due soon, stale deals, creator counts."),
    _schema("scout_creator", "Talent scouting: pull a creator's real numbers (YouTube: subscribers, median recent views, "
            "engagement rate, upload cadence, momentum; Instagram: follower estimate) and save them to the talent tracker. "
            "Accepts a handle like @name or a channel/profile URL. After it returns, give a short fit assessment.",
            {"handle": S, "platform": {**S, "description": "youtube (default) or instagram"}}, ["handle"]),
    _schema("get_brand_voice", "Get the owner's brand voice samples. ALWAYS call this before writing scripts, captions, "
            "hooks, titles or posts for the owner's brand."),
    _schema("save_brand_voice", "Save a sample of the owner's writing (script, caption, post) as a brand voice reference.",
            {"sample": S}, ["sample"]),
    _schema("video_transcript", "Timestamped transcript of a YouTube video ([m:ss] chunks). Use to find clip moments "
            "for Shorts/Reels, quote the video, or summarise it with timestamps.", {"url": S}, ["url"]),
    _schema("save_note", "Save a note, task or idea.",
            {"content": S, "tag": {**S, "description": "task, idea, research, meeting, general"}}, ["content"]),
    _schema("get_notes", "Show the user's saved notes, optionally filtered by tag.", {"tag": S}),
    _schema("delete_note", "Delete a note by ID.", {"note_id": I}, ["note_id"]),
    _schema("calculate", "Math: + - * / // % ** (or ^), parentheses, abs, round, min, max, sum, pow, sqrt, log, log10, "
            "exp, ceil, floor, pi, e.", {"expression": S}, ["expression"]),
    _schema("get_datetime", "Current date and time in the user's timezone."),
    _schema("generate_image", "Create an image and send it to the user (thumbnails, posters, mockups, concept art, "
            "social posts). Write a detailed visual prompt: subject, composition, style, lighting, colours, and any exact "
            "text to render in quotes. aspect: square, landscape (16:9, thumbnails) or portrait (9:16, reels/stories).",
            {"prompt": S, "aspect": {**S, "enum": ["square", "landscape", "portrait"]}}, ["prompt"]),
    _schema("compose_email", "Write an email for the user and give them buttons that open it, pre-filled, in their own "
            "Gmail or Outlook (they press send). Use whenever they want an email drafted, written or replied to. Write "
            "the complete, ready-to-send body in their voice.",
            {"to": S, "subject": S, "body": S, "cc": S}, ["subject", "body"]),
    _schema("calendar_event", "Create a calendar event with buttons for Google Calendar and Outlook plus an .ics file "
            "for Apple Calendar. start/end are ISO date-times in the user's timezone, e.g. 2026-10-16T13:00.",
            {"title": S, "start": S, "end": S, "location": S, "details": S,
             "all_day": {"type": "boolean"}}, ["title", "start"]),
    _schema("my_emails", "The owner's Gmail (read only): recent emails with sender, subject and preview, important and "
            "unread first. query uses Gmail search syntax (from:, subject:, is:unread...); empty = everything except "
            "promotions/social. Summarise; never claim you can send, delete or change email.",
            {"query": S, "hours": I}),
    _schema("my_calendar", "The owner's Google Calendar: events from today for `days` days.", {"days": I}),
    _schema("add_to_calendar", "Add an event to the owner's Google Calendar. First call WITHOUT confirmed to get the "
            "exact event, show it to the user and ask yes/no; only after they say yes call again with confirmed=true. "
            "start/end: ISO date-times in the user's timezone, e.g. 2026-11-06T15:00.",
            {"title": S, "start": S, "end": S, "location": S, "details": S, "confirmed": {"type": "boolean"}},
            ["title", "start"]),
    _schema("run_python", "Your computer: run Python in the user's files folder (everything they sent is there; list it "
            "with analysis_files). Use it to read, analyse, convert, edit or create ANY file: data analysis with charts "
            "(pandas as pd, numpy as np, matplotlib.pyplot as plt are imported; call show('Title') after plotting), "
            "Word (python-docx), PowerPoint (python-pptx), Excel (openpyxl, with formatting/formulas), PDF (fitz = "
            "PyMuPDF: read, merge, split, extract, create), images (PIL), CSV/JSON, zip. Any file you save in the "
            "current folder is sent to the user automatically. print() what you need to see; work step by step and "
            "check the output before answering.", {"code": S}, ["code"]),
    _schema("analysis_files", "Files the user has sent that are available to run_python."),
    _schema("create_pdf", "Make a PDF document and send it: reports, research write-ups, plans, itineraries, comparisons, "
            "study notes, anything the user will keep, print or share, or that is too long for chat. content is "
            "Markdown (headings, lists, tables).", {"title": S, "content": S}, ["title", "content"]),
    _schema("start_task", "Keep working on something in the background and message the user only when there's news: "
            "tracking a story, a launch, prices, a person or company, a search that takes days. Use when they say keep me "
            "posted, track, follow, monitor, keep an eye on, let me know when.",
            {"goal": S, "every_hours": {"type": "number"}, "days": {"type": "number"}}, ["goal"]),
    _schema("list_tasks", "Background tasks currently running for the user."),
    _schema("stop_task", "Stop a background task by its number.", {"task_id": S}, ["task_id"]),
    _schema("whatsapp_message", "Write a WhatsApp message and give a button that opens WhatsApp with it filled in "
            "(to a phone number with country code if given, otherwise they pick the chat).",
            {"text": S, "phone": S}, ["text"]),
]

TALENT_TOOLS = {"save_creator", "find_creators", "save_deal", "pipeline", "scout_creator",
                "get_brand_voice", "save_brand_voice"}
OWNER_ONLY = TALENT_TOOLS | {"my_emails", "my_calendar", "add_to_calendar", "start_task", "list_tasks", "stop_task", "run_python", "analysis_files"} | ({"generate_image"} if not PUBLIC_IMAGE_GEN else set())


def tools_for(is_owner: bool) -> list:
    return [t for t in ALL_TOOLS if is_owner or t["name"] not in OWNER_ONLY]


# Kept for older imports.
TOOLS = ALL_TOOLS


def execute_tool(name: str, inputs: dict) -> str:
    handlers = {
        "web_search": lambda: web_search(inputs["query"]),
        "run_python": lambda: __import__("core.analyst", fromlist=["x"]).run_python(inputs["code"]),
        "analysis_files": lambda: __import__("core.analyst", fromlist=["x"]).list_files(_uid()),
        "create_pdf": lambda: __import__("core.docs", fromlist=["x"]).tool_create_pdf(inputs["title"], inputs["content"]),
        "start_task": lambda: __import__("core.tasks", fromlist=["x"]).start(
            _uid(), context.get().chat_id, inputs["goal"], inputs.get("every_hours") or 6, inputs.get("days") or 7),
        "list_tasks": lambda: __import__("core.tasks", fromlist=["x"]).list_text(_uid()),
        "stop_task": lambda: __import__("core.tasks", fromlist=["x"]).stop(_uid(), inputs["task_id"]),
        "my_emails": lambda: _owner_only() or __import__("core.gsuite", fromlist=["x"]).email_text(
            inputs.get("query", ""), int(inputs.get("hours") or 24)),
        "my_calendar": lambda: _owner_only() or __import__("core.gsuite", fromlist=["x"]).events_text(
            int(inputs.get("days") or 1), _tz().key),
        "add_to_calendar": lambda: _owner_only() or __import__("core.gsuite", fromlist=["x"]).add_event(
            inputs["title"], inputs["start"], inputs.get("end", ""), inputs.get("location", ""), inputs.get("details", ""),
            _tz().key, bool(inputs.get("confirmed"))),
        "read_url": lambda: read_url(inputs["url"]),
        "scan_sources": lambda: scan_sources(inputs["query"]),
        "download_video": lambda: tool_download_video(inputs["url"]),
        "remember": lambda: remember(inputs["fact"]),
        "forget": lambda: forget(inputs["about"]),
        "set_reminder": lambda: set_reminder(inputs["text"], inputs["when"], inputs.get("repeat", "")),
        "list_reminders": list_reminders,
        "cancel_reminder": lambda: cancel_reminder(inputs["reminder_id"]),
        "save_note": lambda: save_note(inputs["content"], inputs.get("tag", "general")),
        "get_notes": lambda: get_notes(inputs.get("tag")),
        "delete_note": lambda: delete_note(inputs["note_id"]),
        "calculate": lambda: calculate(inputs["expression"]),
        "get_datetime": get_datetime,
        "generate_image": lambda: generate_image(inputs["prompt"], inputs.get("aspect", "square")),
        "watch_keyword": lambda: watch_keyword(inputs["keyword"]),
        "list_watches": lambda: watch.list_text(_uid()),
        "unwatch_keyword": lambda: watch.remove(_uid(), inputs["keyword"]),
        "save_creator": lambda: _talent_only() or talent.save_creator(
            inputs["name"], **{k: v for k, v in inputs.items() if k != "name"}),
        "find_creators": lambda: _talent_only() or talent.find(inputs.get("query", ""), inputs.get("status", "")),
        "save_deal": lambda: _talent_only() or talent.save_deal(
            inputs.get("creator", ""), inputs.get("brand", ""), inputs.get("deal_id"),
            **{k: v for k, v in inputs.items() if k not in ("creator", "brand", "deal_id")}),
        "pipeline": lambda: _talent_only() or talent.pipeline_text(),
        "scout_creator": lambda: _talent_only() or __import__("core.scout", fromlist=["scout"]).scout(
            inputs["handle"], inputs.get("platform", "")),
        "get_brand_voice": lambda: _talent_only() or __import__("core.scout", fromlist=["x"]).get_brand_voice(),
        "save_brand_voice": lambda: _talent_only() or __import__("core.scout", fromlist=["x"]).save_brand_voice(inputs["sample"]),
        "video_transcript": lambda: __import__("core.scout", fromlist=["x"]).timestamped_transcript(inputs["url"]),
        "compose_email": lambda: __import__("core.compose", fromlist=["x"]).compose_email(
            inputs.get("to", ""), inputs.get("subject", ""), inputs.get("body", ""), inputs.get("cc", "")),
        "calendar_event": lambda: __import__("core.compose", fromlist=["x"]).calendar_event(
            inputs["title"], inputs["start"], inputs.get("end", ""), inputs.get("location", ""),
            inputs.get("details", ""), bool(inputs.get("all_day"))),
        "whatsapp_message": lambda: __import__("core.compose", fromlist=["x"]).whatsapp_message(
            inputs["text"], inputs.get("phone", "")),
    }
    fn = handlers.get(name)
    return fn() if fn else "Unknown tool."

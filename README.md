# Personal Strategic Agent

A Claude-powered agent on Telegram and Discord. For everyone else it works like ChatGPT, Claude, Gemini or Perplexity (any question, web answers with sources, formatted replies, files, images, voice); the business tools (scouting, content studio, talent tracker, radar, briefings) are the owner's only.

What it does:
- Chat with a learned profile of each user: it asks new users what they're interested in, learns from their messages, files and images, and remembers facts long-term
- Personalised news for every user at 8 AM and 7 PM their local time, plus the owner's full strategic briefing and breaking-news alerts
- Reads links: articles, PDFs, YouTube transcripts
- Downloads videos from X, Instagram, TikTok, YouTube Shorts, Reddit, Facebook, Threads (up to 50 MB)
- Reads files (PDF, DOCX, PPTX, XLSX, CSV, TXT) and images; remembers images for follow-up questions
- Voice notes, transcribed on the machine with faster-whisper (free)
- Reminders (one-off, daily, weekdays, weekly), notes, calculator, web search
- Choice of model at the start of each conversation, kept until 2 hours of quiet or /switch: Claude Haiku 4.5, Sonnet 5.5, Opus 5.5, Fable 5.1; GPT-5.6 Luna, Terra, Sol; Gemini 3.1 Flash-Lite, 3.7 Flash, 3.1 Pro (pricier models use more of a user's daily requests)
- Web dashboard at http://localhost:8090: active sessions, requests and spend per user, spend by model, day and feature, source health
- Group chats, like Meta AI in WhatsApp: add the bot to a Telegram group and it answers when someone @mentions it or replies to it (shared memory per group, public persona, no private tools; owner is notified and can /leave)
- Talent scouting: /scout @channel gives subscribers, median views, engagement, upload cadence and momentum, saved to the tracker
- Content studio: /script, /shorts (clip moments with timestamps from a long video), /captions, all in your brand voice (/brandvoice add)
- Nightly database backup to iCloud Drive (or data/backups), last 7 kept; /backup for now
- Protection against instructions hidden in web pages, posts and transcripts
- Image generation (owner by default)
- Instant keyword alerts every 30 minutes from news sites, Reddit and Hacker News (free, no AI call)
- Talent agency tracker for the owner: creators, brand deals, stages, due dates, stale-deal follow-ups, weekly Monday summary
- Spend tracking per user and feature, a daily budget for other users, and Telegram alerts to the owner when anything breaks
- Daily content and business radar across the owner's topics (creator economy, talent, entertainment, brands, gaming and more), from news, Reddit, X, Instagram, YouTube and competitors

## Run on a Mac

```
cd ~/my-agent
python3 -m venv venv          # first time only
source venv/bin/activate
pip install -r requirements.txt   # first time only
cp .env.example .env              # first time only, then fill in keys
python main.py
```

Then send `/start` to the bot on Telegram. The bot runs only while this terminal stays open and the computer is awake.

## Telegram commands

Everyone: `/start`, `/news`, `/alert <keyword>`, `/alerts`, `/unalert <id>`, `/interests <topics>`, `/keywords`, `/newsoff`, `/newson`, `/profile`, `/forget`, `/clear`, `/help`, `/quick <q>`

Everyone also: `/model` (models and default), `/switch` (pick a different model), `/picker on|off`

Owner: `/deep <q>` (Sonnet 5.5), `/think <q>`, `/thinking <level>`, `/activity [user]`, `/dashboard`, `/credits <balance>`, `/brief` (full briefing), `/radar` (content and business radar), `/pipeline` (talent tracker), `/status` (health), `/usage` (spend), `/users`, `/block <id>`, `/unblock <id>`

Non-owners get `DAILY_LIMIT` requests a day (default 20) and always use Haiku 4.5. The owner has no limit; quick
questions go to Haiku and strategy, writing and long messages to Sonnet 5.5.

## Personality and thinking

- Edit `prompts/owner.md` (how it talks to you) and `prompts/public.md` (everyone else). Changes apply on the next message.
- `/thinking off|low|medium|high` sets how hard models think in your chats; `/think <q>` for one message.
- `/style <...>` lets anyone set their own reply style.

## How it thinks

Every message from you goes through three steps:

1. **Understand.** A fast pass reads the recent conversation and your message and works out what you mean (typos,
   Hinglish, "that", "him"), whether it needs fresh facts, and whether it's hard enough for the deeper model with
   thinking (`core/reason.py`).
2. **Research and answer.** Anything about the world is searched first (Google plus Google News from the last 24
   hours, with dates); the answer is written from those sources.
3. **Fact check.** A second pass checks every claim against the sources fetched for that answer and today's date, and
   removes or corrects anything unsupported. It never guesses what an acronym means.

It also keeps working after the chat: "keep me posted on X" starts a background task that re-researches every few
hours and messages only what's new (`core/tasks.py`), and once a day it tells you your loose ends from recent chats.
Anything that sends, buys, deletes or changes something needs your yes; text inside web pages and emails is treated
as data, never as instructions.

## Evaluations

`evals/cases.json` holds real questions the agent has to get right (searching for unknown acronyms, current events,
no forced creator-economy angle, no search for chit-chat, confirming before adding calendar events...).
`python scripts/eval.py` runs them against the live models in throwaway conversations and prints a pass rate.
Run it before every update; add a case every time the agent gets something wrong.

## Data

Everything is in `data/agent.db` (SQLite): users, learned profiles, facts, notes, reminders, conversations, usage.
Users can see their data with `/profile` and erase it with `/forget`. Every message and tool use (searches, links, downloads) is logged for the owner for `ACTIVITY_DAYS` (default 30); users are told when they start that chats are saved on the owner's hard disk and can be read by the owner. Never commit `data/`.

## Health check

`python selftest.py` checks every live service (Anthropic, Telegram, Serper, Google News, Reddit, Hacker News, links, YouTube transcripts, yt-dlp, voice, database, scheduler) and prints a fix for anything broken.

## Run 24/7

See DEPLOY.md (free Oracle Cloud server).

## Layout

- `main.py`: starts every platform whose keys are present in `.env`
- `core/agent.py`: system prompt and Claude tool loop
- `core/tools.py`: web search, notes, calculator, date/time
- `core/store.py`: SQLite storage
- `core/llm.py`: every AI call (Claude, ChatGPT, Gemini), with cost tracking and budgets
- `core/models.py`: the models users can pick
- `platforms/dashboard.py`: web dashboard
- `core/alerts.py`: Telegram alerts to the owner
- `core/watch.py`: keyword alerts
- `core/talent.py`: talent agency tracker
- `selftest.py`: live health check
- `core/profile.py`: onboarding and learning about users
- `core/news.py`: personalised news
- `core/radar.py`: content radar
- `core/web.py`: link reading
- `core/voice.py`: voice transcription
- `core/context.py`: per-request user context
- `core/downloader.py`: video downloads via yt-dlp (bare links, plus the agent's `download_video` tool)
- `core/memory.py`: per-user conversation memory (`memory.json`)
- `digest.py`: hourly news digest
- `platforms/`: Telegram, Discord and WhatsApp front ends

## Maintenance

- Check API keys: `python check_keys.py`
- Downloads failing: `pip install -U yt-dlp`
- Instagram blocked: put a browser-exported `cookies.txt` in this folder

## Never commit

`.env`, `memory.json`, `covered_news.json`, `cookies.txt`, `venv/` (already in `.gitignore`).

## Keep it running on your Mac

    bash scripts/autostart.sh install     # starts at login, restarts on crash, stops idle sleep
    bash scripts/autostart.sh status      # running? last log lines
    bash scripts/autostart.sh logs        # follow the log
    bash scripts/autostart.sh restart     # after changing code or .env
    bash scripts/autostart.sh uninstall   # back to starting it by hand

Closing the lid still sleeps the Mac. For true 24/7, use a server (DEPLOY.md).

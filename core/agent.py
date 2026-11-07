import os
import re
import time

import anthropic

from core import llm
from datetime import datetime
from core.memory import Memory
from core.tools import execute_tool, tools_for
from core import context, profile, store

client = llm.Client("chat")
memory = Memory()


PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
_prompt_cache = {}


def load_prompt(name: str) -> str:
    """Read prompts/<name>.md, re-reading whenever the file changes (no restart needed)."""
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _prompt_cache.get(name, (0, ""))[1] or f"You are a helpful assistant. Platform: {{platform}}"
    cached = _prompt_cache.get(name)
    if not cached or cached[0] != mtime:
        with open(path, encoding="utf-8") as f:
            _prompt_cache[name] = (mtime, f.read().strip())
    return _prompt_cache[name][1]


def strip_markdown(text: str) -> str:
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*{1,3}', '', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    text = re.sub(r'^[-*•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'-{2,}', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# Models: quick questions go to Haiku, strategy and long work to Sonnet.
REPLY_MAX_TOKENS = int(os.getenv("REPLY_MAX_TOKENS", "2048"))
THINKING_LEVELS = ("off", "low", "medium", "high")
HAIKU_THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 10000}


def thinking_level() -> str:
    """Owner's chosen thinking depth (/thinking), default from THINKING in .env."""
    lvl = store.kv_get("thinking_level") or os.getenv("THINKING", "off")
    return lvl if lvl in THINKING_LEVELS else "off"


FAST_MODEL = "claude-haiku-4-5-20251001"
SMART_MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 6

DEEP_HINTS = (
    "strategy", "strategic", "plan", "analy", "should i", "why", "compare", "evaluate",
    "pitch", "business", "startup", "career", "resume", "linkedin", "interview",
    "write", "draft", "essay", "assignment", "project", "framework", "swot", "pros and cons",
    "brainstorm", "idea", "ideas", "content", "brand", "positioning", "monetiz",
    "negotiat", "invest", "market", "explain", "review", "feedback", "improve",
    "deep", "detailed", "thorough",
)


def pick_model(is_owner: bool, message: str) -> tuple:
    """Return (model, cleaned_message). Prefix a message with /deep or /quick to force a model."""
    text = message.strip()
    low = text.lower()
    if low.startswith("/deep"):
        text = text[5:].strip() or text
        return (SMART_MODEL if is_owner else FAST_MODEL), text
    if low.startswith("/quick"):
        return FAST_MODEL, text[6:].strip() or text
    if not is_owner:
        return FAST_MODEL, text
    if len(text) > 280 or "file content:" in low or any(h in low for h in DEEP_HINTS):
        return SMART_MODEL, text
    return FAST_MODEL, text


def build_system(uid: str, is_owner: bool, platform: str, group: dict = None, speaker: str = "") -> list:
    """Big static prompt is cached (cheap after the first call); time, platform and the
    user's learned profile sit after it."""
    base = load_prompt("owner" if is_owner else "public")
    tz = store.user_tz(store.get_user(uid) or {})
    now = datetime.now(tz).strftime("%A, %B %d %Y, %I:%M %p")
    dynamic = (f"Current time: {now} ({tz.key})\nPlatform: {platform}\n"
               f"Today is {now.split(',')[0]}, {datetime.now(tz):%d %B %Y}. Your own knowledge stops well before today: for anything "
               "that could have happened or changed recently (elections, results, news, prices, schedules, who holds a role), "
               "call web_search first and answer from the newest dated results, never from memory. If results disagree, the "
               "newest wins; say the date of what you report.\n"
               "Answer the question that was asked. Don't bring in the creator economy, my brand, gaming or my career unless I "
               "ask about them or they are clearly part of the question.")
    if group and group.get("inline"):
        dynamic += ("\n\nINLINE MODE: your answer is being posted into a chat between the user and other people "
                    "(they typed @ and your name in that chat). Write a standalone answer the whole chat can read: "
                    "direct, under 120 words unless the question needs more, no follow-up questions, no greetings. "
                    "Don't use download_video, generate_image, calendar_event, reminders or memory tools here.")
    elif group:
        dynamic += (f"\n\nYou are in a Telegram group chat called '{group.get('title') or 'a group'}'. Several people talk "
                    f"here and you only see messages where someone mentions you or replies to you. The person asking now "
                    f"is {speaker or 'a group member'}. Keep replies short and conversational, address them by name when "
                    "natural, and never share anyone's private information in the group.")
    else:
        style = (store.get_user(uid) or {}).get("style")
        if style:
            dynamic += f"\n\nHOW THIS USER WANTS REPLIES (follow this): {style}"
        known = profile.profile_block(uid)
        if known:
            dynamic += "\n\n" + known
        from core import newslinks
        recent = newslinks.recent_block(uid)
        if recent:
            dynamic += "\n\n" + recent
    from core import safety
    return [
        {"type": "text", "text": base + "\n\n" + safety.RULES, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "credit balance" in msg or "billing" in msg:
        return ("I'm out of Anthropic API credits, so I can't answer right now. "
                "Top up at console.anthropic.com > Billing.")
    if isinstance(e, anthropic.AuthenticationError):
        return "My Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env (run check_keys.py)."
    if isinstance(e, anthropic.RateLimitError):
        return "I'm being rate limited by Anthropic. Try again in a minute."
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "I couldn't reach Anthropic. Check the internet connection and try again."
    if isinstance(e, anthropic.InternalServerError):
        return "Anthropic is having problems right now. Try again in a few minutes."
    return f"Something went wrong: {str(e)[:200]}"


def _openai_tools(tools: list) -> list:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def _summarise_args(args: dict) -> str:
    for k in ("query", "url", "keyword", "prompt", "text", "fact", "name", "expression", "content", "creator"):
        if args.get(k):
            return str(args[k])
    return ", ".join(f"{k}={v}" for k, v in args.items())[:300]


TOOL_LABELS = {
    "web_search": "Searching the web: {query}", "scan_sources": "Checking news, Reddit, X and YouTube: {query}",
    "read_url": "Reading {host}", "video_transcript": "Getting the video transcript",
    "download_video": "Downloading the video", "generate_image": "Drawing the image",
    "scout_creator": "Pulling creator numbers: {handle}", "set_reminder": "Setting the reminder",
    "calculate": "Calculating", "remember": "Saving that to memory", "watch_keyword": "Setting up the alert",
}


def tool_label(name: str, args: dict) -> str:
    from urllib.parse import urlparse
    a = dict(args or {})
    a["host"] = (urlparse(a.get("url", "")).hostname or "the page").replace("www.", "")
    try:
        label = TOOL_LABELS.get(name, "Working on it").format(**{k: str(v)[:60] for k, v in a.items()})
    except (KeyError, IndexError):
        label = TOOL_LABELS.get(name, "Working on it").split(":")[0]
    return label


def _exec_tool(name: str, args: dict) -> str:
    from core import safety
    c = context.get()
    c.progress = tool_label(name, args)
    store.log_activity(c.uid, c.platform, f"tool:{name}", _summarise_args(args or {}), c.model_label)
    try:
        return safety.wrap(name, execute_tool(name, args))
    except Exception as e:
        return f"Tool error: {str(e)[:200]}"


def _run_tools(calls: list) -> list:
    """Run the tools the model asked for; several at once run in parallel (each keeps the user's context)."""
    if len(calls) <= 1:
        return [_exec_tool(n, a) for n, a in calls]
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, _exec_tool, n, a) for n, a in calls]
        return [f.result() for f in futures]


def _thinking_kwargs(model: str, level: str) -> dict:
    if level == "off":
        return {}
    if model.startswith("claude-haiku"):
        budget = HAIKU_THINKING_BUDGET[level]
        return {"thinking": {"type": "enabled", "budget_tokens": budget}, "max_tokens": budget + REPLY_MAX_TOKENS}
    # Sonnet 5.5 / Opus 5.5 and newer: adaptive thinking with an effort level
    return {"thinking": {"type": "adaptive"}, "extra_body": {"output_config": {"effort": level}},
            "max_tokens": REPLY_MAX_TOKENS + 8000}


def _cached(messages: list) -> list:
    """The same messages with a cache breakpoint on the last block, so the conversation so far (and earlier tool
    rounds) is read from Anthropic's prompt cache on the next call instead of being processed again: faster replies
    and about a tenth of the input cost. The stored history is left untouched."""
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content.strip():
            return messages
        last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        blocks = list(content)
        b = blocks[-1]
        b = dict(b) if isinstance(b, dict) else b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else None
        if not b or b.get("type") in ("thinking", "redacted_thinking"):
            return messages
        b["cache_control"] = {"type": "ephemeral"}
        blocks[-1] = b
        last["content"] = blocks
    else:
        return messages
    return messages[:-1] + [last]


NEWS_RE = re.compile(r"\b(news|latest|newest|recent(ly)?|today|tonight|yesterday|this (week|morning|evening)|right now|"
                     r"currently|current|update[sd]?|what('s| is) happening|happening|breaking|headlines?|score[sd]?|"
                     r"results?|won|wins?|elections?|midterms?|polls?|prices?|stock|market|live)\b", re.I)


# Messages that never need the web: chit-chat, reminders, notes, maths, writing from what I gave, my own data.
NO_SEARCH_RE = re.compile(r"^\s*(hi|hey|hello|yo|thanks|thank you|ok|okay|cool|nice|good (morning|night)|gm|gn|bye)\b|"
                          r"\b(remind me|reminder|my notes?|save (a )?note|note this|calculate|convert \d|rewrite|rephrase|"
                          r"proofread|translate|summari[sz]e this|my (emails?|inbox|calendar|schedule)|schedule (a|an|my)|"
                          r"remember that|forget|download|transcript|plan my|my (day|week)|write (a|an|my) (email|message|caption|post|tweet|reply)|"
                          r"draft (a|an|my))\b", re.I)


def wants_search(text: str) -> bool:
    """Search first unless the message clearly doesn't need the web. Facts, names, acronyms, news, 'keep me posted',
    anything about the world: search. Answering from memory is what made answers stale."""
    t = (text or "").strip()
    if not t or re.search(r"https?://", t) or len(t) < 8:
        return False
    if NEWS_RE.search(t):
        return True
    return not NO_SEARCH_RE.search(t)


def _last_user_text(messages) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list) and not any((b.get("type") if isinstance(b, dict) else getattr(b, "type", "")) == "tool_result" for b in c):
            return " ".join((b.get("text") or "") if isinstance(b, dict) else (getattr(b, "text", "") or "") for b in c)
        return ""
    return ""


def _run_anthropic(uid, model, system, tools, messages, level="off") -> str:
    # Asking for news or anything current: search first, every time (the model can't skip it and answer from memory)
    force = any(t.get("name") == "web_search" for t in tools or []) and wants_search(_last_user_text(messages))
    for _ in range(MAX_TOOL_ROUNDS + 1):
        last_round = _ == MAX_TOOL_ROUNDS
        choice = {"type": "none"} if last_round else {"type": "auto"}
        if force and _ == 0:
            if level == "off":
                choice = {"type": "tool", "name": "web_search"}
            else:   # thinking can't be combined with a forced tool: switch thinking off for this first step
                level, choice = "off", {"type": "tool", "name": "web_search"}
        kwargs = dict(_uid=uid, model=model, max_tokens=REPLY_MAX_TOKENS, system=system, tools=tools,
                      tool_choice=choice, messages=_cached(messages))
        kwargs.update(_thinking_kwargs(model, level))
        c = context.get()
        try:
            if c.stream and hasattr(client.messages, "stream"):
                streamed = []

                def on_text(delta, c=c, streamed=streamed):
                    streamed.append(delta)
                    c.partial = "".join(streamed)
                response = client.messages.stream(on_text, **kwargs)
            else:
                response = client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if level != "off" and any(w in str(e).lower() for w in ("thinking", "effort", "output_config")):
                from core import alerts
                alerts.notify(f"thinking:{model}", f"{model} rejected the thinking setting, so it answered without "
                              f"thinking: {str(e)[:150]}", 720)
                level = "off"
                continue
            raise
        if response.stop_reason == "tool_use" and not last_round:
            c.partial = ""  # text before a tool call ("Let me check") isn't the answer
            calls = [b for b in response.content if b.type == "tool_use"]
            results = _run_tools([(b.name, b.input) for b in calls])
            tool_results = [{"type": "tool_result", "tool_use_id": b.id, "content": r} for b, r in zip(calls, results)]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue
        return "\n".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip() or "No response."
    return "I got stuck in a loop of tool calls. Try rephrasing."


def _run_openai_compat(uid, provider, model, system, tools, history, level="off") -> str:
    """ChatGPT and Gemini: same tools, OpenAI-style function calling."""
    import json as _json
    oc = llm.OpenAICompatClient(provider, "chat")
    sys_text = "\n\n".join(b["text"] for b in system)
    messages = [{"role": "system", "content": sys_text}] + [
        {"role": m["role"], "content": m["content"] if isinstance(m["content"], str) else str(m["content"])}
        for m in history
    ]
    otools = _openai_tools(tools)
    limit_key = "max_completion_tokens" if provider == "openai" else "max_tokens"
    for _ in range(MAX_TOOL_ROUNDS + 1):
        last_round = _ == MAX_TOOL_ROUNDS
        kwargs = {"model": model, "messages": messages, limit_key: REPLY_MAX_TOKENS + (4000 if level != "off" else 0)}
        if not last_round:
            first_news = _ == 0 and any(t.get("name") == "web_search" for t in tools or []) and wants_search(_last_user_text(history))
            kwargs.update(tools=otools, tool_choice={"type": "function", "function": {"name": "web_search"}} if first_news else "auto")
        if level != "off":
            kwargs["reasoning_effort"] = level
        try:
            resp = oc.create(_uid=uid, **kwargs)
        except Exception as e:
            if level != "off" and "reason" in str(e).lower():
                level = "off"
                kwargs.pop("reasoning_effort", None)
                resp = oc.create(_uid=uid, **kwargs)
            else:
                raise
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if calls and not last_round:
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id or f"call_{i}", "type": "function",
                                             "function": {"name": c.function.name,
                                                          "arguments": c.function.arguments or "{}"}}
                                            for i, c in enumerate(calls)]})
            for i, c in enumerate(calls):
                try:
                    args = _json.loads(c.function.arguments or "{}")
                except Exception:
                    args = {}
                result = _exec_tool(c.function.name, args)
                messages.append({"role": "tool", "tool_call_id": c.id or f"call_{i}", "content": str(result)})
            continue
        return (msg.content or "").strip() or "No response."
    return "I got stuck in a loop of tool calls. Try rephrasing."


CLAUDE_OUT_MINUTES = 30   # after a credit error, skip Claude for this long, then try it again


def _is_credit_error(e: Exception) -> bool:
    m = str(e).lower()
    return "credit balance" in m or ("billing" in m and "anthropic" in m)


def _claude_out() -> bool:
    at = store.kv_get("claude_out_at") or 0
    return time.time() - at < CLAUDE_OUT_MINUTES * 60


def _fallback_model():
    """Cheapest ready non-Claude model, used when Anthropic credits run out."""
    from core import models
    ready = models.available()
    for key in ("gemini-lite", "gemini", "gpt-luna", "gpt"):
        if key in ready and ready[key]["provider"] != "anthropic":
            return ready[key]
    return None


def provider_error(e: Exception, provider: str) -> str:
    from core import alerts
    name = {"openai": "OpenAI (ChatGPT)", "gemini": "Google (Gemini)"}.get(provider, provider)
    msg = str(e).lower()
    status = getattr(e, "status_code", None)
    if status in (401, 403) or "api key" in msg or "unauthorized" in msg:
        alerts.notify(f"{provider}_auth", f"{name} rejected the API key. Check it in .env.", 240)
        return f"{name} rejected my API key, so that model isn't working right now. Pick another model."
    if status == 429 or "quota" in msg or "insufficient" in msg or "billing" in msg:
        alerts.notify(f"{provider}_quota", f"{name} says quota or billing is exhausted: {str(e)[:150]}", 240)
        return f"{name} is out of quota or rate limited right now. Pick another model."
    if status == 404 or "model" in msg and "not" in msg:
        alerts.notify(f"{provider}_model", f"{name} doesn't recognise the model name in .env: {str(e)[:150]}", 240)
        return f"That {name} model isn't available. Pick another model."
    return f"{name} had a problem: {str(e)[:150]}. Pick another model or try again."


def chat(user_id: str, platform: str, message: str, ctx: "context.Ctx" = None,
         name: str = "", username: str = "", chat_id: str = "", model_key: str = None,
         think: str = None, group: dict = None) -> str:
    """Run one turn. user_id is the platform's raw id; pass a context.Ctx to receive files
    queued by tools (ctx.outbox). model_key picks a model from core.models ('auto' or None = routing)."""
    from core import models
    real_owner = store.is_owner(platform, user_id)
    is_owner = real_owner and not group  # groups always get the public persona and tools
    uid = store.uid_for(platform, user_id)
    store.ensure_user(uid, platform, chat_id or (user_id if platform == "telegram" else ""), name, username)
    if ctx is None:
        ctx = context.Ctx()
    ctx.uid, ctx.platform, ctx.is_owner = uid, platform, is_owner
    ctx.chat_id = chat_id or str(user_id)
    context.set_ctx(ctx)

    model, message = pick_model(is_owner, message)
    provider = "anthropic"
    chosen = models.available().get(model_key or "")
    if chosen:
        model, provider = chosen["id"], chosen["provider"]
    ctx.model_label = (chosen or models.registry().get(models.key_for_id(model)) or {}).get("label", model)

    level = (think or thinking_level()) if is_owner else "off"
    speaker = name or (f"@{username}" if username else "someone")
    store.log_activity(uid, "telegram_group" if group else platform,
                       "message", (f"[{group.get('title')}] " if group else "") + message, ctx.model_label)

    mem_key = f"group:{group['id']}" if group else uid
    history = memory.get(mem_key, platform)
    history.append({"role": "user", "content": f"{speaker}: {message}" if group else message})
    system = build_system(uid, is_owner, platform, group, speaker)
    tools = tools_for(is_owner)

    fallback = _fallback_model() if provider == "anthropic" and _claude_out() else None
    if fallback:  # Claude credits ran out recently: go straight to the backup model
        model, provider = fallback["id"], fallback["provider"]
        ctx.model_label = fallback["label"]
    try:
        try:
            if provider == "anthropic":
                final_text = _run_anthropic(uid, model, system, tools, history.copy(), level)
            else:
                final_text = _run_openai_compat(uid, provider, model, system, tools, history, level)
        except anthropic.APIError as e:
            backup = _fallback_model()
            if not (_is_credit_error(e) and backup):
                raise
            store.kv_set("claude_out_at", time.time())
            from core import alerts
            alerts.notify("anthropic_credits", "Anthropic credits are used up. I've switched everyone to "
                          f"{backup['label']} until you top up at console.anthropic.com > Billing.", 120)
            model, provider = backup["id"], backup["provider"]
            ctx.model_label = backup["label"]
            final_text = _run_openai_compat(uid, provider, model, system, tools, history, level)
        if is_owner:
            final_text = strip_markdown(final_text)  # your prompt asks for plain text
        ctx.rich = not is_owner  # everyone else gets formatted replies (bold, lists, code, links)
        history.append({"role": "assistant", "content": final_text})
        memory.save(mem_key, platform, history)
        if not group:
            profile.note_message(uid, history)
        store.update_user(uid, last_model=ctx.model_label)
        return final_text
    except llm.BudgetExceeded as e:
        if is_owner:
            return "You've hit your own daily budget (OWNER_DAILY_BUDGET_USD). Raise it in .env if needed."
        if str(e) == "user":
            return "You've used today's free allowance. It resets at midnight. See you tomorrow!"
        return "I'm taking a short break right now. Please try again later."
    except anthropic.APIError as e:
        from core import alerts
        if _is_credit_error(e):
            store.kv_set("claude_out_at", time.time())
            alerts.notify("anthropic_credits", "Anthropic API credits are used up. The agent can't answer anyone. "
                          "Top up at console.anthropic.com > Billing.", 120)
        elif isinstance(e, anthropic.AuthenticationError):
            alerts.notify("anthropic_auth", "Anthropic rejected the API key. Check ANTHROPIC_API_KEY.", 1440)   # once a day, not every 2h
        if not is_owner and _is_credit_error(e):
            return "I'm temporarily unavailable. Please try again later."
        return friendly_error(e)
    except Exception as e:
        if provider != "anthropic":
            return provider_error(e, provider)
        return f"Something went wrong: {str(e)[:200]}"

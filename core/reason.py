"""Thinking around each owner turn, in the spirit of agents that "get what you mean" and don't make things up:

1. understand(): before answering, a fast pass reads the recent conversation and the new message and works out what is
   actually meant (typos, Hinglish, "that", "him", half sentences), whether it needs fresh facts, and whether it's
   hard enough for the deeper model with thinking.
2. verify(): after a factual answer, a second pass checks every claim against the sources fetched in this turn and
   today's date, and removes or corrects anything the sources don't support.

Both use Haiku: about a cent for both passes on a typical message. Owner only (REASONING=false turns them off).
"""
import json
import os
import re
from datetime import datetime

from core import llm

ON = os.getenv("REASONING", "true").lower() == "true"
MODEL = "claude-haiku-4-5-20251001"
client = llm.Client("reason")


def _text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join((b.get("text") or "") if isinstance(b, dict) else (getattr(b, "text", "") or "") for b in c)
    return ""


def understand(history: list, message: str, now: str) -> dict:
    """{'intent': str, 'search': bool, 'deep': bool, 'queries': [str]} or {} if it fails (the turn still runs)."""
    if not ON:
        return {}
    recent = [m for m in history[-8:] if m.get("role") in ("user", "assistant") and _text(m.get("content")).strip()]
    convo = "\n".join(f"{'ME' if m['role'] == 'user' else 'AGENT'}: {_text(m['content'])[:500]}" for m in recent)
    prompt = f"""Today is {now}. You read my messages to a personal assistant and work out what I actually mean.

Recent conversation:
{convo or '(none)'}

New message from me: {message}

Reply with JSON only:
{{"intent": "one or two sentences: exactly what I want, with pronouns, typos, slang and Hinglish resolved, using the conversation for context",
  "search": true if answering needs facts about the world that could be new or that you aren't certain of (news, people, companies, prices, acronyms, events, anything after 2025), false for chit-chat, writing from what I gave, maths, my own reminders/notes/email/calendar,
  "deep": true if it needs real analysis, planning, strategy, comparison or careful reasoning; false for quick facts, chit-chat and simple tasks,
  "queries": ["up to 3 precise web search queries that would answer it, with the year if relevant"]}}"""
    try:
        r = client.messages.create(_uid="owner", model=MODEL, max_tokens=300, messages=[{"role": "user", "content": prompt}])
        raw = "".join(getattr(b, "text", "") for b in r.content)
        j = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        return {"intent": str(j.get("intent", ""))[:600], "search": bool(j.get("search")), "deep": bool(j.get("deep")),
                "queries": [str(q)[:120] for q in (j.get("queries") or [])][:3]}
    except Exception as e:
        print(f"understand() skipped: {str(e)[:100]}")
        return {}


def verify(question: str, answer: str, evidence: list, now: str) -> str:
    """The answer with unsupported or outdated claims fixed; the original if nothing needs fixing or the check fails."""
    if not ON or not evidence or not answer or len(answer) < 80:
        return answer
    ev = "\n\n---\n\n".join(e[:4000] for e in evidence)[:16000]
    prompt = f"""Today is {now}. You are a strict fact checker for a personal assistant.

QUESTION: {question}

DRAFT ANSWER:
{answer}

SOURCES FETCHED FOR THIS ANSWER (the only ground truth; newer dated items beat older ones):
{ev}

Check every factual claim in the draft (names, numbers, dates, who/what/when, what something stands for).
- Claim supported by the sources: keep it.
- Claim contradicted by the sources: correct it.
- Claim not in the sources and not common, stable knowledge: remove it, or say plainly that it couldn't be confirmed.
- Anything stated as upcoming that the sources show has already happened (or the reverse): fix the tense and facts.
Keep the draft's wording, tone, length and plain-text style; don't add a preamble, notes or a list of changes.
Output only the final answer."""
    try:
        r = client.messages.create(_uid="owner", model=MODEL, max_tokens=2500, messages=[{"role": "user", "content": prompt}])
        out = "".join(getattr(b, "text", "") for b in r.content).strip()
        return out if len(out) > 40 else answer
    except Exception as e:
        print(f"verify() skipped: {str(e)[:100]}")
        return answer


def now_text(tz) -> str:
    return datetime.now(tz).strftime("%A %d %B %Y, %I:%M %p %Z")

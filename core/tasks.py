"""Background tasks and loose ends: the agent keeps working after the chat ends.

- start_task("track the CPT situation", every_hours=6, days=7): every few hours the agent researches the goal again
  and messages only what's new since its last report (silent when nothing changed).
- list_tasks / stop_task: what it's working on, and stopping a job.
- loose_ends(): once a day it reads your recent chats and reminders and tells you what's still open (things you said
  you'd do, deadlines you mentioned, questions left hanging) that no reminder covers.
"""
import json
import os
import time

from core import context, llm, store

MAX_TASKS = int(os.getenv("MAX_BG_TASKS", "6"))
LOOSE_ENDS_TIME = os.getenv("LOOSE_ENDS_TIME", "19:00")    # owner's local time; empty = off
MODEL = "claude-haiku-4-5-20251001"
client = llm.Client("tasks")


def _all() -> list:
    return store.kv_get("bg_tasks") or []


def _save(ts: list):
    store.kv_set("bg_tasks", ts)


def start(uid: str, chat_id: str, goal: str, every_hours: float = 6, days: float = 7) -> str:
    ts = [t for t in _all() if t["until"] > time.time()]
    if len([t for t in ts if t["uid"] == uid]) >= MAX_TASKS:
        return f"You already have {MAX_TASKS} background tasks. Stop one first (list_tasks)."
    every = max(1.0, float(every_hours or 6))
    tid = max([t["id"] for t in ts] + [0]) + 1
    ts.append({"id": tid, "uid": uid, "chat_id": chat_id, "goal": goal.strip()[:300], "every": every,
               "until": time.time() + max(0.1, float(days or 7)) * 86400, "next": time.time() + every * 3600,
               "last_report": "", "runs": 0})
    _save(ts)
    return (f"Background task #{tid} started: '{goal[:120]}'. I'll check every {every:g} h for {float(days or 7):g} days "
            "and message you only when there's something new.")


def list_text(uid: str) -> str:
    ts = [t for t in _all() if t["uid"] == uid and t["until"] > time.time()]
    if not ts:
        return "No background tasks running."
    return "\n".join(f"#{t['id']}: {t['goal']} (every {t['every']:g} h, {max(0, (t['until'] - time.time()) / 86400):.1f} days "
                     f"left, {t['runs']} checks)" for t in ts)


def stop(uid: str, tid) -> str:
    ts = _all()
    keep = [t for t in ts if not (t["uid"] == uid and str(t["id"]) == str(tid).lstrip("#"))]
    _save(keep)
    return "Stopped." if len(keep) < len(ts) else f"No task #{tid}."


def _research(t: dict) -> str:
    from core import agent
    ctx = context.Ctx(uid=t["uid"], platform="telegram", chat_id=t["chat_id"], is_owner=(t["uid"] == "owner"))
    context.set_ctx(ctx)
    ctx.force_search = True
    tools = [x for x in agent.tools_for(ctx.is_owner) if x["name"] in ("web_search", "read_url", "scan_sources")]
    from core import reason
    now = reason.now_text(store.user_tz(store.get_user(t["uid"]) or {}))
    msg = (f"Background task you are running for me: {t['goal']}\nToday is {now}.\n"
           f"Your previous report (don't repeat it): {t['last_report'] or '(none yet)'}\n"
           "Research it now. Reply with only what is NEW since the previous report, as at most 6 short plain-text lines, "
           "each with its date. If nothing meaningful is new, reply exactly: NOTHING_NEW")
    system = [{"type": "text", "text": "You are a precise research agent. Use only facts from your searches; never guess."}]
    out = agent._run_anthropic(t["uid"], MODEL, system, tools, [{"role": "user", "content": msg}], "off")
    if out.strip().startswith("NOTHING_NEW"):
        return ""
    return reason.verify(t["goal"], out, ctx.evidence, now)


def run_due(send):
    """Scheduler job: run background tasks that are due. send(text, chat_id)."""
    ts, changed = _all(), False
    for t in ts:
        if t["until"] < time.time() or t["next"] > time.time():
            continue
        t["next"], t["runs"], changed = time.time() + t["every"] * 3600, t["runs"] + 1, True
        try:
            report = _research(t)
        except Exception as e:
            print(f"Background task {t['id']} failed: {e}")
            continue
        if report:
            t["last_report"] = report[:1500]
            send(f"UPDATE (task #{t['id']}: {t['goal'][:80]})\n{report}", t["chat_id"])
    live = [t for t in ts if t["until"] > time.time()]
    if changed or len(live) != len(ts):
        _save(live)


def loose_ends(send):
    """Daily: open loops from the owner's recent chats that no reminder covers."""
    from core import reason
    from core.memory import Memory
    hist = []
    for plat in ("telegram", "discord"):
        try:
            hist += Memory().get("owner", plat)[-40:]
        except Exception:
            pass
    lines = [f"{'ME' if m['role'] == 'user' else 'AGENT'}: {reason._text(m.get('content'))[:300]}" for m in hist
             if m.get("role") in ("user", "assistant") and reason._text(m.get("content")).strip()]
    if len(lines) < 4:
        return
    rem = "; ".join(r.get("text", "") for r in (store.list_reminders("owner") or []))[:1500]
    now = reason.now_text(store.user_tz(store.get_user("owner") or {}))
    prompt = (f"Today is {now}. Below are my recent chats with my assistant, and my active reminders.\n\n"
              + "\n".join(lines[-60:]) + f"\n\nActive reminders: {rem or 'none'}\n\n"
              "List only real open loops I should not forget: things I said I'd do, deadlines or dates I mentioned, "
              "decisions I left pending, questions I asked that weren't resolved. Skip anything a reminder already "
              "covers and anything finished. At most 5 lines, plain text, each starting with '- '. "
              "If there are none, reply exactly: NONE")
    try:
        r = client.messages.create(_uid="owner", model=MODEL, max_tokens=400, messages=[{"role": "user", "content": prompt}])
        out = "".join(getattr(b, "text", "") for b in r.content).strip()
    except Exception as e:
        print(f"Loose ends failed: {e}")
        return
    if out and not out.startswith("NONE"):
        send("LOOSE ENDS\n" + out + "\n\nReply 'remind me ...' to set any of these.", None)

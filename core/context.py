"""Who the agent is talking to right now.

Uses contextvars, so each request (each thread started by asyncio.to_thread) sees its
own user. Tools read this instead of globals, so two users chatting at the same time
can never see each other's notes, reminders or files.
"""
import contextvars
from dataclasses import dataclass, field


@dataclass
class Ctx:
    uid: str = ""            # 'owner' or 'telegram:<id>'
    platform: str = ""
    chat_id: str = ""        # where files/reminders go
    is_owner: bool = False
    outbox: list = field(default_factory=list)  # files queued by tools: {"kind": "video"|"photo", ...}
    model_label: str = ""    # which model answered (set by core.agent.chat)
    rich: bool = False       # reply contains Markdown to render as Telegram formatting
    # Live progress for the chat UI (set from the worker thread, read by the Telegram side):
    progress: str = ""       # e.g. "Searching the web: IPL auction"
    partial: str = ""        # the answer so far, while it streams
    stream: bool = False     # ask the model to stream its final answer into `partial`
    buttons: list = field(default_factory=list)  # (label, url) link buttons to show under the reply
    evidence: list = field(default_factory=list)  # what search/read tools returned this turn (for the fact check)
    force_search: object = None                  # True/False from core.reason.understand; None = use the keyword rule


_current = contextvars.ContextVar("agent_ctx", default=None)


def set_ctx(ctx: Ctx):
    _current.set(ctx)


def get() -> Ctx:
    c = _current.get()
    if c is None:
        c = Ctx()
        _current.set(c)
    return c

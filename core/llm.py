"""Every Claude call goes through here: it records the dollar cost per user and feature,
and enforces a daily budget for everyone except the owner.

Usage in a module:
    from core import llm
    client = llm.Client("news")
    client.messages.create(model=..., messages=..., _uid="telegram:123")
"""
import os

import anthropic

from core import context, store

# Price per million tokens (input, output). Cache reads cost 10% of input, cache writes 125%.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-4-5": (3.0, 15.0),
}
DEFAULT_PRICE = (2.0, 10.0)

# Daily dollar budget for everyone except the owner (their chats, news, learning, images).
PUBLIC_DAILY_BUDGET = float(os.getenv("PUBLIC_DAILY_BUDGET_USD", "0.50"))
# Hard stop for the owner too, as a safety net against runaway loops. 0 disables.
OWNER_DAILY_BUDGET = float(os.getenv("OWNER_DAILY_BUDGET_USD", "3.00"))
# One person can't use more than this per day, so nobody can burn everyone else's share.
PER_USER_DAILY = float(os.getenv("PER_USER_DAILY_USD", "0.10"))
# When the estimated Anthropic balance (/credits) drops below this, other users pause so the owner keeps working.
CREDIT_RESERVE = float(os.getenv("CREDIT_RESERVE_USD", "1.00"))
LOW_BALANCE_ALERT = float(os.getenv("LOW_BALANCE_ALERT_USD", "3.00"))


class BudgetExceeded(Exception):
    pass


def price_for(model: str) -> tuple:
    from core import models
    p = models.price_for_id(model)
    if p:
        return p
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return DEFAULT_PRICE


def cost_of(model: str, usage) -> tuple:
    pin, pout = price_for(model)
    u = {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    cost = (u["input"] * pin + u["output"] * pout + u["cache_read"] * pin * 0.1
            + u["cache_write"] * pin * 1.25) / 1_000_000
    return cost, u


def check_budget(uid: str):
    from core import alerts
    if uid == "owner":
        if OWNER_DAILY_BUDGET and store.spend_today(owner=True) >= OWNER_DAILY_BUDGET:
            alerts.notify("owner_budget", f"Your own usage hit ${OWNER_DAILY_BUDGET:.2f} today, so I've paused. "
                          "Raise OWNER_DAILY_BUDGET_USD in .env if that was expected.", 360)
            raise BudgetExceeded("owner")
        return
    est = store.credit_estimate()
    if est is not None:
        how = (f"This is my estimate, not Anthropic's figure: the ${est['entered']:.2f} you gave me with /credits on "
               f"{est['set_at'][:10]}, minus ${est['entered'] - est['left']:.2f} I've used since. If you've topped up, "
               "send /credits <balance shown at console.anthropic.com > Billing> and this resets.")
        if est["left"] < LOW_BALANCE_ALERT:
            alerts.notify("low_balance", f"Anthropic balance is about ${est['left']:.2f}. {how}", 720)
        if est["left"] < CREDIT_RESERVE:
            alerts.notify("reserve", f"Estimated balance is under ${CREDIT_RESERVE:.2f}, so I've paused replies to "
                          f"other people (you're not affected) to save what's left for you. {how}", 360)
            raise BudgetExceeded("reserve")
    if PER_USER_DAILY and store.spend_today_user(uid) >= PER_USER_DAILY:
        raise BudgetExceeded("user")
    spent = store.spend_today(owner=False)
    if spent >= PUBLIC_DAILY_BUDGET:
        alerts.notify("public_budget", f"Other users hit today's ${PUBLIC_DAILY_BUDGET:.2f} budget. They're paused "
                      "until midnight. Change PUBLIC_DAILY_BUDGET_USD in .env to adjust.", 720)
        raise BudgetExceeded("public")
    if spent >= 0.8 * PUBLIC_DAILY_BUDGET:
        alerts.notify("public_budget_80", f"Other users have used 80% of today's ${PUBLIC_DAILY_BUDGET:.2f} budget.", 720)


class _Messages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, _uid: str = None, **kwargs):
        uid = _uid or context.get().uid or "owner"  # background jobs count as the owner's
        check_budget(uid)
        resp = self._owner._client.messages.create(**kwargs)
        try:
            cost, usage = cost_of(kwargs.get("model", ""), resp.usage)
            store.record_spend(uid, self._owner.feature, kwargs.get("model", ""), usage, cost)
        except Exception as e:  # never fail a reply because bookkeeping failed
            print(f"Spend tracking failed: {e}")
        return resp


    def stream(self, on_text, _uid: str = None, **kwargs):
        """Like create(), but calls on_text(delta) as the answer is written. Falls back to create()."""
        raw = self._owner._client.messages
        if not hasattr(raw, "stream"):
            return self.create(_uid=_uid, **kwargs)
        uid = _uid or context.get().uid or "owner"
        check_budget(uid)
        with raw.stream(**kwargs) as s:
            for delta in s.text_stream:
                try:
                    on_text(delta)
                except Exception:
                    pass
            resp = s.get_final_message()
        try:
            cost, usage = cost_of(kwargs.get("model", ""), resp.usage)
            store.record_spend(uid, self._owner.feature, kwargs.get("model", ""), usage, cost)
        except Exception as e:
            print(f"Spend tracking failed: {e}")
        return resp


class Client:
    def __init__(self, feature: str):
        self.feature = feature
        self._client = anthropic.Anthropic()
        self.messages = _Messages(self)


class OpenAICompatClient:
    """ChatGPT (OpenAI) or Gemini (Google's OpenAI-compatible endpoint), with the same cost
    tracking and budgets as Claude."""

    def __init__(self, provider: str, feature: str = "chat"):
        import openai
        from core import models
        self.provider = provider
        self.feature = feature
        if provider == "gemini":
            self._client = openai.OpenAI(api_key=os.getenv("GEMINI_API_KEY"), base_url=models.GEMINI_BASE_URL)
        else:
            self._client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def create(self, _uid: str = None, **kwargs):
        uid = _uid or context.get().uid or "owner"
        check_budget(uid)
        resp = self._client.chat.completions.create(**kwargs)
        try:
            u = resp.usage
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            usage = {"input": (u.prompt_tokens or 0) - cached, "output": u.completion_tokens or 0,
                     "cache_read": cached, "cache_write": 0}
            pin, pout = price_for(kwargs.get("model", ""))
            cost = (usage["input"] * pin + usage["output"] * pout + cached * pin * 0.1) / 1_000_000
            store.record_spend(uid, self.feature, kwargs.get("model", ""), usage, cost)
        except Exception as e:
            print(f"Spend tracking failed: {e}")
        return resp

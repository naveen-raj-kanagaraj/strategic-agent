"""Evaluation suite: real questions the agent must handle correctly. Run before every update.

  python scripts/eval.py            # all cases (uses real API calls: roughly $0.10-0.30 per run)
  python scripts/eval.py acronym    # only cases whose id contains "acronym"

Each case in evals/cases.json can require tools to be called (expect_tools), forbid tools (forbid_tools), and require
or forbid patterns in the answer (must_match / must_not_match, regular expressions). Your real chat history is never
touched: each case runs in a throwaway conversation. Add a case every time the agent gets something wrong.
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

from core import agent, context  # noqa: E402


class _Mem:          # throwaway memory per case
    def __init__(self):
        self.h = {}

    def get(self, uid, plat):
        return list(self.h.get((uid, plat), []))

    def save(self, uid, plat, history):
        self.h[(uid, plat)] = list(history)


def run(filter_=""):
    cases = json.load(open(os.path.join(ROOT, "evals", "cases.json")))
    cases = [c for c in cases if filter_ in c["id"]]
    owner = os.getenv("OWNER_TELEGRAM_ID")
    if not owner:
        sys.exit("OWNER_TELEGRAM_ID missing in .env")
    calls = []
    real_exec = agent._exec_tool

    def spy(name, args):
        calls.append(name)
        if name in ("set_reminder", "add_to_calendar", "start_task", "save_note", "remember", "watch_keyword"):
            return f"(eval: {name} recorded, not executed)"     # no side effects during tests
        return real_exec(name, args)
    agent._exec_tool = spy
    passed, rows = 0, []
    for c in cases:
        agent.memory = _Mem()
        calls.clear()
        t0 = time.time()
        try:
            ans = agent.chat(owner, "telegram", c["message"], context.Ctx(), chat_id=owner)
        except Exception as e:
            ans = f"ERROR {e}"
        fails = [f"didn't call {t}" for t in c.get("expect_tools", []) if t not in calls]
        fails += [f"called {t}" for t in c.get("forbid_tools", []) if t in calls]
        fails += [f"missing /{p}/" for p in c.get("must_match", []) if not re.search(p, ans)]
        fails += [f"contains /{p}/" for p in c.get("must_not_match", []) if re.search(p, ans)]
        ok = not fails
        passed += ok
        rows.append((c["id"], ok, fails, time.time() - t0, ans))
        print(f"{'PASS' if ok else 'FAIL'}  {c['id']:<26} {time.time() - t0:5.1f}s  tools={calls}  {'; '.join(fails)}")
        if not ok:
            print("      answer: " + ans[:300].replace("\n", " "))
    print(f"\n{passed}/{len(cases)} passed ({100 * passed / max(len(cases), 1):.0f}%)")
    return passed == len(cases)


if __name__ == "__main__":
    sys.exit(0 if run(sys.argv[1] if len(sys.argv) > 1 else "") else 1)

import ast
import asyncio
import copy
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from copilot_shadow import hook, store
from copilot_shadow.decide import decide

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "copilot_shadow"


def make_user(**kw):
    d = dict(stage="conversation", manual_mode=False, blocked=False)
    d.update(kw)
    return SimpleNamespace(**d)


def make_cs(intent="question"):
    return {"stage": "conversation", "conversation": {"intent": intent, "sentiment": 0.5,
            "complexity": 0.1, "messages_count": 3, "last_message": "SECRET TEXT"},
            "identity": {"username": "uname", "name": "Juan", "region": "MX"},
            "external": {"priority": "LOW", "is_gamer": True}, "memory": {"a": 1},
            "risk": {"self_exclusion": False, "minor": False}}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.pool import StaticPool
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        store._ready.clear()
        hook._seen.clear()
        self.env = mock.patch.dict(os.environ, {"COPILOT_SHADOW_ENABLED": "true"})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.engine.dispose()

    async def drain(self):
        while hook._tasks:
            await asyncio.gather(*list(hook._tasks), return_exceptions=True)

    async def count(self):
        from sqlalchemy.exc import OperationalError
        try:
            async with self.sf() as s:
                return (await s.execute(select(func.count()).select_from(store.shadow_decisions))).scalar()
        except OperationalError:
            return 0  # table not created: nothing was written

    def sched(self, msg=1, **kw):
        args = dict(session_factory=self.sf, chat_id=111, message_id=msg, user=make_user(),
                    client_state=make_cs(), history=[{}, {}], regime="casual", reply_text="hola")
        args.update(kw)
        return hook.schedule_shadow(**args)


class TestDecide(unittest.TestCase):
    def snap(self, **kw):
        s = dict(stage="conversation", intent="question", risk={}, manual_mode=False, blocked=False)
        s.update(kw)
        return s

    def test_rules(self):
        self.assertEqual(decide(self.snap())["chosen_action"], "CONTINUE_DIALOGUE")
        self.assertEqual(decide(self.snap(intent="objection"))["chosen_action"], "HANDLE_OBJECTION")
        self.assertEqual(decide(self.snap(stage="offer"))["chosen_action"], "PRESENT_OFFER")
        self.assertEqual(decide(self.snap(intent="ready"))["chosen_action"], "PRESENT_OFFER")
        self.assertEqual(decide(self.snap(intent="ready", stage="new"))["chosen_action"], "CONTINUE_DIALOGUE")
        self.assertEqual(decide(self.snap(stage="deposited", intent="objection"))["chosen_action"], "CONTINUE_DIALOGUE")
        self.assertEqual(decide(self.snap(risk={"minor": True}))["chosen_action"], "HUMAN_HANDOFF")
        self.assertEqual(decide(self.snap(blocked=True))["chosen_action"], "WAIT")
        d = decide(self.snap())
        self.assertEqual(d["decision_status"], "pre_uplift")
        self.assertIn("WAIT", [c["action"] for c in d["candidate_actions"]])


if __name__ == "__main__":
    unittest.main()

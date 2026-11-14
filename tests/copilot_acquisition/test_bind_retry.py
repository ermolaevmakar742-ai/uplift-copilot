"""Regression: lead_not_ready must be retry-safe (delayed client_id), with no duplicate leads and first/last touch intact."""
import asyncio
import unittest

from sqlalchemy import text

from copilot_acquisition import intake, store
from tests.copilot_acquisition.test_acquisition import Base


async def _drain():
    while intake._tasks:
        await asyncio.gather(*list(intake._tasks))


class TestBindRetry(Base):
    async def bind(self, token, chat, **k):
        return await store.bind_token(self.sf, token, chat, wait_s=0.01, retries=2, **k)

    async def run_intake(self, token, chat, delays):
        key = intake.C.hash_token(token)
        await intake._bind(self.sf, token, chat, delays=delays)
        return key

    async def set_client(self, chat, cid):
        async with self.sf() as s:
            await s.execute(text("UPDATE users SET client_id = :c WHERE id = :i"), {"c": cid, "i": chat}); await s.commit()

    async def test_1_immediate_client_id(self):
        _, t = await self.click()
        await self.run_intake(t, 1001, ())
        self.assertEqual((await self.q("SELECT status, lead_ref FROM acquisition_clicks"))[0]["status"], "BOUND")

    async def test_2_delayed_client_id_binds_on_retry(self):
        _, t = await self.click()
        orig = store.bind_token

        async def fast(sf, token, chat, **k):
            return await orig(sf, token, chat, retries=1, wait_s=0)

        async def late_client():
            await asyncio.sleep(0.05); await self.set_client(1003, "cli_cccc")
        from unittest import mock
        with mock.patch.object(store, "bind_token", fast):
            await asyncio.gather(self.run_intake(t, 1003, (0.03, 0.05, 0.05, 0.1)), late_client())
        (c,) = await self.q("SELECT status, lead_ref FROM acquisition_clicks")
        self.assertEqual((c["status"], c["lead_ref"]), ("BOUND", "cli_cccc"))
        self.assertEqual(len(await self.q("SELECT * FROM acquisition_leads")), 1)
        self.assertEqual(intake._inflight, set())

    async def test_3_client_id_never_appears(self):
        _, t = await self.click()
        from unittest import mock
        orig = store.bind_token
        with mock.patch.object(store, "bind_token", lambda sf, tk, ch, **k: orig(sf, tk, ch, retries=1, wait_s=0)):
            await self.run_intake(t, 1003, (0.01, 0.01))
        self.assertEqual((await self.q("SELECT status FROM acquisition_clicks"))[0]["status"], "HANDOFF_GENERATED")
        self.assertEqual(await self.q("SELECT * FROM acquisition_leads"), [])
        self.assertEqual(intake._inflight, set())
        await self.set_client(1003, "cli_cccc")                 # still bindable later; expire_unbound semantics untouched
        self.assertEqual(await self.bind(t, 1003), "bound")

    async def test_4_repeat_same_marker(self):
        _, t = await self.click()
        await self.run_intake(t, 1001, ()); await self.run_intake(t, 1001, ())
        self.assertEqual(await self.bind(t, 1001), "already_bound")
        (lead,) = await self.q("SELECT * FROM acquisition_leads")
        self.assertEqual(lead["touches"], 1)

    async def test_5_two_tokens_same_new_lead_concurrently(self):
        _, t1 = await self.click(); c2, t2 = await self.click(await self.campaign(source="instagram", creative_id="cr_02"))
        r = await asyncio.gather(self.bind(t1, 1001), self.bind(t2, 1001))
        self.assertEqual(r, ["bound", "bound"])
        (lead,) = await self.q("SELECT * FROM acquisition_leads")
        self.assertEqual(lead["touches"], 2)
        self.assertNotEqual(lead["first_click_id"], lead["last_click_id"])
        self.assertEqual(len(await self.q("SELECT * FROM acquisition_leads")), 1)

    async def test_6_existing_bound_lead_unchanged(self):
        c1, t1 = await self.click(); _, t2 = await self.click()
        await self.bind(t1, 1001)
        before = (await self.q("SELECT * FROM acquisition_leads"))[0]
        self.assertEqual(await self.bind(t1, 1001), "already_bound")
        after = (await self.q("SELECT * FROM acquisition_leads"))[0]
        self.assertEqual(dict(before), dict(after))
        self.assertEqual(await self.bind(t2, 1001), "bound")
        lead = (await self.q("SELECT * FROM acquisition_leads"))[0]
        self.assertEqual((lead["first_campaign_id"], lead["touches"]), (c1["campaign_id"], 2))


if __name__ == "__main__":
    unittest.main()

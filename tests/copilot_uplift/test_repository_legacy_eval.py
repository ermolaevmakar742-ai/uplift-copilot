import unittest
from datetime import datetime, timezone

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from copilot_uplift.migrate import ensure_schema
from copilot_uplift.repository import EvaluationRepository, UpliftCorrupt
from copilot_uplift.store import uplift_evaluations


class LegacyEvaluationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await ensure_schema(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.repo = EvaluationRepository(self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_latest_never_returns_legacy_v1_evaluation(self):
        async with self.sessions() as session:
            await session.execute(insert(uplift_evaluations).values(
                evaluation_id="ue_legacy_v1",
                evaluation_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                policy_version="uplift-policy.v1",
                data_quality="CAUSAL_READY",
                dataset_version="ds_legacy",
                contract_version="uplift_evaluation.v1",
                payload={"contract_version": "uplift_evaluation.v1"},
            ))
            await session.commit()

        self.assertIsNone(await self.repo.latest())

    async def test_explicit_legacy_read_fails_closed(self):
        async with self.sessions() as session:
            await session.execute(insert(uplift_evaluations).values(
                evaluation_id="ue_legacy_v1",
                evaluation_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                policy_version="uplift-policy.v1",
                data_quality="CAUSAL_READY",
                dataset_version="ds_legacy",
                contract_version="uplift_evaluation.v1",
                payload={"contract_version": "uplift_evaluation.v1"},
            ))
            await session.commit()

        with self.assertRaises(UpliftCorrupt):
            await self.repo.get("ue_legacy_v1")


if __name__ == "__main__":
    unittest.main()

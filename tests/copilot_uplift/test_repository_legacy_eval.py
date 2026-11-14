import unittest
from datetime import datetime, timezone

from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from copilot_uplift.migrate import ensure_schema
from copilot_uplift.repository import EvaluationRepository, UpliftCorrupt
from copilot_uplift.store import uplift_assignments, uplift_evaluations


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

    async def test_sqlite_database_blocks_assignment_update_and_delete(self):
        async with self.sessions() as session:
            await session.execute(insert(uplift_assignments).values(
                assignment_id="ua_test",
                chat_id=101,
                arm="CONTROL",
                assignment_probability=0.1,
                decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                context="test",
                contract_version="uplift_assignment.v1",
                payload={"assignment_id": "ua_test"},
            ))
            await session.commit()

        with self.assertRaises(IntegrityError):
            async with self.sessions() as session:
                await session.execute(update(uplift_assignments).where(
                    uplift_assignments.c.assignment_id == "ua_test"
                ).values(context="rewritten"))
                await session.commit()

        with self.assertRaises(IntegrityError):
            async with self.sessions() as session:
                await session.execute(delete(uplift_assignments).where(
                    uplift_assignments.c.assignment_id == "ua_test"
                ))
                await session.commit()

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

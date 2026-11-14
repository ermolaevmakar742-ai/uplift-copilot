"""Explicit, additive, idempotent migration for decision_evidence (touches no other table).

    python3 -m copilot_evidence.migrate               # dry run: prints the SQL it would run
    python3 -m copilot_evidence.migrate --apply       # creates table, unique constraint, immutability trigger
    python3 -m copilot_evidence.migrate --rollback    # prints the rollback SQL (dry run)
    python3 -m copilot_evidence.migrate --rollback --apply   # DROPS decision_evidence (and its trigger)

Never run implicitly: not on import, not by the repository, not by production code.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.schema import CreateTable

from copilot_evidence import store


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(store.metadata.create_all)          # CREATE TABLE IF NOT EXISTS
        if engine.dialect.name == "postgresql":
            for ddl in store.APPEND_ONLY_DDL:
                await conn.execute(text(ddl))


async def rollback_schema(engine) -> None:
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            for ddl in store.ROLLBACK_DDL:
                await conn.execute(text(ddl))
        else:
            await conn.execute(text("DROP TABLE IF EXISTS decision_evidence"))


def plan(dialect) -> list[str]:
    out = [str(CreateTable(store.decision_evidence, if_not_exists=True).compile(dialect=dialect)).strip()]
    if dialect.name == "postgresql":
        out += [" ".join(d.split()) for d in store.APPEND_ONLY_DDL]
    return out


def rollback_plan(dialect) -> list[str]:
    return list(store.ROLLBACK_DDL) if dialect.name == "postgresql" else ["DROP TABLE IF EXISTS decision_evidence"]


async def _main(apply: bool, rollback: bool) -> int:
    from dotenv import load_dotenv
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(here, ".env"))
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    for stmt in (rollback_plan if rollback else plan)(engine.dialect):
        print(stmt, end=";\n\n")
    if apply:
        await (rollback_schema if rollback else ensure_schema)(engine)
        print("ROLLED BACK" if rollback else "APPLIED")
    else:
        print("DRY RUN (use --apply)")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main("--apply" in sys.argv, "--rollback" in sys.argv)))

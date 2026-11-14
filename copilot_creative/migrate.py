"""Explicit, additive, idempotent migration for the cr_* tables (touches no other table).

    python3 -m copilot_creative.migrate                      # dry run: prints the SQL
    python3 -m copilot_creative.migrate --apply              # CREATE TABLE/INDEX IF NOT EXISTS
    python3 -m copilot_creative.migrate --rollback --apply   # DROPS the five cr_* tables
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex, CreateTable

from copilot_creative import store


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(store.metadata.create_all)


def plan(dialect) -> list[str]:
    out = []
    for t in store.TABLES:
        out.append(str(CreateTable(t, if_not_exists=True).compile(dialect=dialect)).strip())
        out += [str(CreateIndex(i, if_not_exists=True).compile(dialect=dialect)).strip() for i in sorted(t.indexes, key=lambda i: i.name)]
    return out


async def _main(apply: bool, rollback: bool) -> int:
    from dotenv import load_dotenv
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(here, ".env"))
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    stmts = list(store.ROLLBACK_DDL) if rollback else plan(engine.dialect)
    for s in stmts:
        print(s, end=";\n\n")
    if apply:
        if rollback:
            async with engine.begin() as conn:
                for s in stmts:
                    await conn.execute(text(s))
        else:
            await ensure_schema(engine)
        print("APPLIED")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main("--apply" in sys.argv, "--rollback" in sys.argv)))

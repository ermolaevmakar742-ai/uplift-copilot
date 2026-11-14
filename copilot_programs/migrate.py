"""Explicit, additive, idempotent migration for `partner_programs` (touches no other table).

    python3 -m copilot_programs.migrate                      # dry run: prints the SQL
    python3 -m copilot_programs.migrate --apply              # creates the table and index
    python3 -m copilot_programs.migrate --rollback           # prints the rollback SQL (dry run)
    python3 -m copilot_programs.migrate --rollback --apply   # DROPS partner_programs

Never run implicitly: not on import, not by the service, the API or production code.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex, CreateTable

from copilot_programs import store


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(store.metadata.create_all)          # CREATE TABLE/INDEX IF NOT EXISTS


async def rollback_schema(engine) -> None:
    async with engine.begin() as conn:
        for ddl in store.ROLLBACK_DDL:
            await conn.execute(text(ddl))


def plan(dialect) -> list[str]:
    t = store.partner_programs
    return ([str(CreateTable(t, if_not_exists=True).compile(dialect=dialect)).strip()]
            + [str(CreateIndex(i, if_not_exists=True).compile(dialect=dialect)).strip()
               for i in sorted(t.indexes, key=lambda i: i.name)])


def rollback_plan(dialect) -> list[str]:
    return list(store.ROLLBACK_DDL)


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

"""Explicit, additive, idempotent migration for production_decisions.

    python3 -m copilot_decisions.migrate            # dry run: prints what it would do
    python3 -m copilot_decisions.migrate --apply    # creates table, indexes, append-only trigger

Never run implicitly: not on import, not by the recorder.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex, CreateTable

from copilot_decisions import store


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(store.metadata.create_all)          # CREATE TABLE/INDEX IF NOT EXISTS
        if engine.dialect.name == "postgresql":
            for ddl in store.APPEND_ONLY_DDL:
                await conn.execute(text(ddl))


def plan(dialect) -> list[str]:
    t = store.production_decisions
    out = [str(CreateTable(t, if_not_exists=True).compile(dialect=dialect)).strip()]
    out += [str(CreateIndex(i, if_not_exists=True).compile(dialect=dialect)).strip() for i in t.indexes]
    if dialect.name == "postgresql":
        out += [" ".join(d.split()) for d in store.APPEND_ONLY_DDL]
    return out


async def _main(apply: bool) -> int:
    from dotenv import load_dotenv
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(here, ".env"))
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    for stmt in plan(engine.dialect):
        print(stmt, end=";\n\n")
    if apply:
        await ensure_schema(engine)
        print("APPLIED")
    else:
        print("DRY RUN (use --apply)")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main("--apply" in sys.argv)))

"""Explicit, additive, idempotent migration for the three uplift tables (touches no other table).

    python3 -m copilot_uplift.migrate                      # dry run: prints the SQL
    python3 -m copilot_uplift.migrate --apply              # creates tables, indexes, immutability triggers
    python3 -m copilot_uplift.migrate --rollback           # prints the rollback SQL (dry run)
    python3 -m copilot_uplift.migrate --rollback --apply   # DROPS the three uplift tables

Never run implicitly: not on import, not by the repository, the service, the API or production code.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex, CreateTable

from copilot_uplift import store


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(store.metadata.create_all)
        if engine.dialect.name == "postgresql":
            for ddl in store.APPEND_ONLY_DDL:
                await conn.execute(text(ddl))
        elif engine.dialect.name == "sqlite":
            for ddl in store.SQLITE_APPEND_ONLY_DDL:
                await conn.execute(text(ddl))


async def rollback_schema(engine) -> None:
    async with engine.begin() as conn:
        for ddl in rollback_plan(engine.dialect):
            await conn.execute(text(ddl))


def plan(dialect) -> list[str]:
    out = []
    for t in sorted(store.metadata.tables.values(), key=lambda t: t.name):
        out.append(str(CreateTable(t, if_not_exists=True).compile(dialect=dialect)).strip())
        out += [str(CreateIndex(i, if_not_exists=True).compile(dialect=dialect)).strip() for i in sorted(t.indexes, key=lambda i: i.name)]
    if dialect.name == "postgresql":
        out += [" ".join(d.split()) for d in store.APPEND_ONLY_DDL]
    return out


def rollback_plan(dialect) -> list[str]:
    if dialect.name == "postgresql":
        return list(store.ROLLBACK_DDL)
    return [f"DROP TABLE IF EXISTS {t}" for t in store.TABLES]


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

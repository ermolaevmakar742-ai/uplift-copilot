import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_programs import migrate, store
from copilot_programs.contract import Program, ProgramValidationError
from copilot_programs.repository import ProgramExists, ProgramNotFound, ProgramRepository
from copilot_programs.service import ProgramService

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

GOOD = {"name": "Casino X MX", "brand": "CasinoX", "countries": ["mx"], "currency": "mxn",
        "registration_url": "https://casinox.example/reg?ref=abc", "promo_code": "WELCOME50",
        "min_deposit": 100, "min_withdrawal": "200.50", "withdrawal_time": "up to 24h",
        "payment_methods": ["OXXO", "SPEI", "OXXO"], "terms": "Wager x30\nNew players only",
        "support_contact": "@casinox_support", "notes": "internal"}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(minutes=1)
        return self.t


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.clock = Clock()
        self.svc = ProgramService(self.sf, self.clock)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def mk(self, pid="casinox-mx", active=False, **over):
        return await self.svc.create_program(pid, {**GOOD, **over}, updated_by="admin", active=active)


class MigrationTests(Base):
    async def test_schema(self):
        async with self.engine.connect() as c:
            cols = await c.run_sync(lambda sc: {x["name"]: x for x in inspect(sc).get_columns("partner_programs")})
            pk = await c.run_sync(lambda sc: inspect(sc).get_pk_constraint("partner_programs"))
        self.assertEqual(set(cols), {"program_id", "name", "brand", "countries", "currency", "registration_url",
                                     "promo_code", "min_deposit", "min_withdrawal", "withdrawal_time",
                                     "payment_methods", "terms", "support_contact", "notes", "active",
                                     "created_at", "updated_at", "updated_by"})
        self.assertEqual(pk["constrained_columns"], ["program_id"])
        for n in ("name", "brand", "countries", "payment_methods", "active", "created_at", "updated_at", "updated_by"):
            self.assertFalse(cols[n]["nullable"], n)

    async def test_dry_run_sql_and_isolation(self):
        pg = postgresql.dialect()
        sql = " ".join(migrate.plan(pg))
        self.assertIn("CREATE TABLE IF NOT EXISTS partner_programs", sql)
        self.assertIn("JSONB", sql)
        self.assertIn("NUMERIC(18, 2)", sql)
        self.assertNotRegex(sql.upper(), r"ALTER TABLE|FOREIGN KEY|REFERENCES|PRODUCTION_DECISIONS|OUTCOMES|DECISION_EVIDENCE|USERS")
        self.assertEqual(set(store.metadata.tables), {"partner_programs"})
        self.assertEqual(migrate.plan(pg), migrate.plan(pg))
        self.assertEqual(migrate.rollback_plan(pg), ["DROP TABLE IF EXISTS partner_programs"])

    async def test_idempotent_apply_and_rollback_only_own_table(self):
        await migrate.ensure_schema(self.engine)
        await migrate.ensure_schema(self.engine)
        async with self.sf() as s:
            await s.execute(text("CREATE TABLE other_table (x int)"))
            await s.commit()
        await migrate.rollback_schema(self.engine)
        async with self.sf() as s:
            names = {r[0] for r in (await s.execute(text("select name from sqlite_master where type='table'"))).all()}
        self.assertEqual(names, {"other_table"})

    async def test_no_implicit_ddl(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        svc = ProgramService(async_sessionmaker(eng, expire_on_commit=False))
        with self.assertRaises(Exception):
            await svc.list_programs()
        await eng.dispose()


class CrudTests(Base):
    async def test_create_and_read(self):
        p = await self.mk()
        self.assertEqual((p.countries, p.currency, p.min_deposit, p.min_withdrawal), (("MX",), "MXN", Decimal("100.00"), Decimal("200.50")))
        self.assertEqual(p.payment_methods, ("OXXO", "SPEI"))
        self.assertFalse(p.active)
        got = await self.svc.get_program("casinox-mx")
        self.assertEqual(got, p)
        self.assertEqual(got.created_at, got.updated_at)
        self.assertEqual(got.updated_by, "admin")
        self.assertEqual(got.terms, "Wager x30\nNew players only")
        json.dumps(got.to_dict())                                    # API-serialisable

    async def test_missing_program(self):
        self.assertIsNone(await self.svc.get_program("nope"))
        self.assertIsNone(await self.svc.get_program(None))
        self.assertIsNone(await self.svc.get_active_program("nope"))
        for fn in (self.svc.activate_program, self.svc.deactivate_program):
            with self.assertRaises(ProgramNotFound):
                await fn("nope", updated_by="admin")
        with self.assertRaises(ProgramNotFound):
            await self.svc.update_program("nope", {"name": "x"}, updated_by="admin")

    async def test_duplicate_id_rejected(self):
        await self.mk()
        with self.assertRaises(ProgramExists):
            await self.mk(name="Other")
        self.assertEqual((await self.svc.get_program("casinox-mx")).name, "Casino X MX")

    async def test_update_changes_fields_stamps_and_keeps_created(self):
        p = await self.mk()
        u = await self.svc.update_program("casinox-mx", {"promo_code": "NEW100", "min_deposit": 50}, updated_by="ops")
        self.assertEqual((u.promo_code, u.min_deposit, u.updated_by), ("NEW100", Decimal("50.00"), "ops"))
        self.assertGreater(u.updated_at, p.updated_at)                 # updated_at refreshed
        self.assertEqual(u.created_at, p.created_at)                   # created_at immutable
        self.assertEqual((u.name, u.brand, u.countries, u.terms), (p.name, p.brand, p.countries, p.terms))  # untouched
        self.assertEqual(await self.svc.get_program("casinox-mx"), u)
        c = await self.svc.update_program("casinox-mx", {"promo_code": None, "notes": ""}, updated_by="ops")
        self.assertEqual((c.promo_code, c.notes), (None, None))        # clearing an optional field works

    async def test_update_rejects_unknown_and_readonly_fields(self):
        await self.mk()
        for bad in ({"program_id": "x"}, {"active": True}, {"updated_by": "me"}, {"created_at": "x"}, {"bogus": 1}):
            with self.assertRaises(ProgramValidationError):
                await self.svc.update_program("casinox-mx", bad, updated_by="admin")

    async def test_activate_deactivate(self):
        p = await self.mk()
        a = await self.svc.activate_program("casinox-mx", updated_by="ops1")
        self.assertTrue(a.active)
        self.assertEqual((a.updated_by, a.name), ("ops1", p.name))
        self.assertGreater(a.updated_at, p.updated_at)
        d = await self.svc.deactivate_program("casinox-mx", updated_by="ops2")
        self.assertFalse(d.active)
        self.assertEqual(d.updated_by, "ops2")
        self.assertGreater(d.updated_at, a.updated_at)
        self.assertFalse((await self.svc.get_program("casinox-mx")).active)

    async def test_cannot_activate_or_save_active_without_minimum_data(self):
        await self.svc.create_program("draft", {"name": "Draft"}, updated_by="admin")      # inactive drafts may be partial
        with self.assertRaises(ProgramValidationError) as cm:
            await self.svc.activate_program("draft", updated_by="admin")
        self.assertEqual(set(cm.exception.errors), {"brand", "registration_url"})   # countries/currency optional since the Account Offer Manager
        self.assertFalse((await self.svc.get_program("draft")).active)
        with self.assertRaises(ProgramValidationError):
            await self.svc.create_program("act", {"name": "A"}, updated_by="admin", active=True)
        await self.mk("full", active=True)
        for patch in ({"registration_url": None}, {"brand": " "}, {"name": None}):
            with self.assertRaises(ProgramValidationError, msg=str(patch)):
                await self.svc.update_program("full", patch, updated_by="admin")          # active program stays complete
        self.assertEqual((await self.svc.get_program("full")).registration_url, GOOD["registration_url"])
        await self.svc.deactivate_program("full", updated_by="admin")
        await self.svc.update_program("full", {"registration_url": None}, updated_by="admin")   # fine while inactive
        with self.assertRaises(ProgramValidationError):
            await self.svc.activate_program("full", updated_by="admin")

    async def test_list(self):
        await self.mk("b-prog", brand="Beta", countries=["MX", "CO"])
        await self.mk("a-prog", active=True, brand="Alpha", countries=["AR"], currency="ars")
        await self.mk("c-prog", brand="Gamma", countries=["MX"])
        self.assertEqual([p.program_id for p in await self.svc.list_programs()], ["a-prog", "b-prog", "c-prog"])
        self.assertEqual([p.program_id for p in await self.svc.list_programs(active=True)], ["a-prog"])
        self.assertEqual([p.program_id for p in await self.svc.list_programs(active=False)], ["b-prog", "c-prog"])
        self.assertEqual([p.program_id for p in await self.svc.list_programs(country="mx")], ["b-prog", "c-prog"])
        self.assertEqual([p.program_id for p in await self.svc.list_programs(brand="ALPHA")], ["a-prog"])
        self.assertEqual(await self.svc.list_programs(country="ZZ"), [])


class ActiveLookupTests(Base):
    async def test_inactive_never_returned_as_active(self):
        await self.mk()
        self.assertIsNone(await self.svc.get_active_program("casinox-mx"))
        self.assertIsNone(await self.svc.get_active_program_for_context(country="MX"))
        self.assertEqual(await self.svc.find_active_programs_for_context(country="MX"), [])
        await self.svc.activate_program("casinox-mx", updated_by="admin")
        self.assertEqual((await self.svc.get_active_program("casinox-mx")).program_id, "casinox-mx")
        await self.svc.deactivate_program("casinox-mx", updated_by="admin")
        self.assertIsNone(await self.svc.get_active_program("casinox-mx"))

    async def test_no_programs_means_none(self):
        self.assertIsNone(await self.svc.get_active_program_for_context())
        self.assertIsNone(await self.svc.get_active_program_for_context(country="MX", brand="x", currency="MXN"))

    async def test_context_matching_multiple_programs(self):
        await self.mk("mx-a", active=True, brand="Alpha", countries=["MX"], currency="mxn")
        await self.mk("co-b", active=True, brand="Beta", countries=["CO"], currency="cop")
        await self.mk("mx-off", brand="Gamma", countries=["MX"], currency="mxn")           # inactive: ignored
        self.assertEqual((await self.svc.get_active_program_for_context(country="mx")).program_id, "mx-a")
        self.assertEqual((await self.svc.get_active_program_for_context(country="CO", currency="COP")).program_id, "co-b")
        self.assertEqual((await self.svc.get_active_program_for_context(brand="beta")).program_id, "co-b")
        self.assertIsNone(await self.svc.get_active_program_for_context(country="CO", currency="MXN"))
        self.assertIsNone(await self.svc.get_active_program_for_context(country="AR"))
        self.assertIsNone(await self.svc.get_active_program_for_context())                  # 2 active -> ambiguous -> None
        self.assertEqual([p.program_id for p in await self.svc.find_active_programs_for_context()], ["co-b", "mx-a"])

    async def test_ambiguity_returns_none_not_a_guess(self):
        await self.mk("mx-1", active=True, countries=["MX"])
        await self.mk("mx-2", active=True, countries=["MX"], brand="Other")
        self.assertIsNone(await self.svc.get_active_program_for_context(country="MX"))
        self.assertEqual(len(await self.svc.find_active_programs_for_context(country="MX")), 2)
        self.assertEqual((await self.svc.get_active_program_for_context(country="MX", brand="other")).program_id, "mx-2")

    async def test_deterministic(self):
        for i in (3, 1, 2):
            await self.mk(f"p{i}", active=True, brand=f"B{i}", countries=["MX"])
        a = [p.program_id for p in await self.svc.find_active_programs_for_context(country="MX")]
        b = [p.program_id for p in await self.svc.find_active_programs_for_context(country="MX")]
        self.assertEqual(a, b)
        self.assertEqual(a, ["p1", "p2", "p3"])


class ValidationTests(Base):
    async def bad(self, **over):
        with self.assertRaises(ProgramValidationError) as cm:
            await self.svc.create_program("vtest", {**GOOD, **over}, updated_by="admin")
        return cm.exception.errors

    async def test_program_id_slug(self):
        for pid in ("", "A", "UPPER", "has space", "x" * 65, "a;b", "-start", "../etc", None, 5, "ü"):
            with self.assertRaises(ProgramValidationError, msg=repr(pid)):
                await self.svc.create_program(pid, GOOD, updated_by="admin")

    async def test_url(self):
        for u in ("javascript:alert(1)", "ftp://x.example/a", "data:text/html,hi", "//casinox.example", "http://",
                  "https://nodot", "https://user:pw@casinox.example/", "https://casinox.example/a b",
                  "https://casinox.example/\nx", "https://" + "a" * 2050 + ".com", "casinox.example"):
            self.assertIn("registration_url", await self.bad(registration_url=u), u[:30])
        for i, u in enumerate(("http://casinox.example", "https://sub.casinox.example/path?a=1&b=2#x")):
            await self.svc.create_program(f"url-{i}", {**GOOD, "registration_url": u}, updated_by="admin")

    async def test_numeric_fields(self):
        for f in ("min_deposit", "min_withdrawal"):
            for v in (-1, "abc", float("nan"), float("inf"), "1e999", 1.234, True, [], {}, 10 ** 10):
                self.assertIn(f, await self.bad(**{f: v}), (f, v))
            for i, v in enumerate((None, "", 0, "0", 12, 12.5, "7.25")):
                await self.svc.create_program(f"n-{f}-{i}", {**GOOD, f: v}, updated_by="admin")

    async def test_currency_and_countries(self):
        for v in ("MX", "MXNN", "12", "mx1"):
            self.assertIn("currency", await self.bad(currency=v))
        for v in (["MEX"], ["M"], ["1x"], [5], "MX;DROP"):
            self.assertIn("countries", await self.bad(countries=v))
        p = await self.svc.create_program("cc", {**GOOD, "countries": "mx, co ,MX"}, updated_by="admin")
        self.assertEqual(p.countries, ("CO", "MX"))

    async def test_text_fields_and_lists(self):
        self.assertIn("name", await self.bad(name="x" * 121))
        self.assertIn("name", await self.bad(name="bad\x00name"))
        self.assertIn("name", await self.bad(name="line\nbreak"))                       # single-line field
        self.assertIn("terms", await self.bad(terms="x" * 4001))
        self.assertIn("payment_methods", await self.bad(payment_methods=["x" * 61]))
        self.assertIn("payment_methods", await self.bad(payment_methods=[1]))
        self.assertIn("payment_methods", await self.bad(payment_methods=["m"] * 0 + [f"m{i}" for i in range(31)]))
        self.assertIn("updated_by", await self.bad_by(""))
        self.assertIn("_unknown", await self.bad(program_id="x", extra=1))

    async def bad_by(self, by):
        with self.assertRaises(ProgramValidationError) as cm:
            await self.svc.create_program("vtest", GOOD, updated_by=by)
        return cm.exception.errors

    async def test_all_errors_reported_at_once_and_nothing_saved(self):
        errs = await self.bad(name="", brand=None, currency="x", min_deposit=-5, registration_url="nope")
        self.assertTrue({"currency", "min_deposit", "registration_url"} <= set(errs))
        self.assertIsNone(await self.svc.get_program("vtest"))

    async def test_sql_injection_input_is_inert(self):
        evil = "x'); DROP TABLE partner_programs;--"
        p = await self.svc.create_program("inj-test", {**GOOD, "name": evil, "notes": "' OR '1'='1", "terms": evil,
                                                       "promo_code": "';--", "payment_methods": [evil]}, updated_by="admin")
        got = await self.svc.get_program("inj-test")
        self.assertEqual((got.name, got.notes, got.promo_code, got.payment_methods), (evil, "' OR '1'='1", "';--", (evil,)))
        self.assertEqual(await self.svc.get_program(evil), None)
        self.assertEqual(await self.svc.list_programs(country=evil, brand="' OR 1=1 --"), [])
        self.assertIsNone(await self.svc.get_active_program_for_context(country=evil, brand=evil, currency=evil))
        with self.assertRaises(ProgramValidationError):
            await self.svc.create_program("x'; DROP TABLE partner_programs;--", GOOD, updated_by="admin")
        self.assertEqual(len(await self.svc.list_programs()), 1)                          # table intact, no extra rows
        async with self.sf() as s:
            self.assertEqual((await s.execute(text("select count(*) from partner_programs"))).scalar(), 1)


class RepositoryIsolationTests(Base):
    async def test_repository_is_only_partner_programs(self):
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / "copilot_programs"
        for f in root.glob("*.py"):
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for n in ast.walk(tree):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    top = m.split(".")[0]
                    self.assertNotIn(top, {"main", "telethon", "openai", "anthropic", "requests", "httpx", "aiohttp",
                                           "socket", "subprocess", "pickle", "copilot_decisions", "copilot_shadow", "copilot_external",
                                           "copilot_evidence", "copilot_outcomes", "client_state", "scout", "language_layer"},
                                     f"{f.name}: {m}")
                    if f.name != "api.py":
                        self.assertNotIn(top, {"fastapi", "starlette"}, f"{f.name}: {m}")
                if isinstance(n, ast.Call):
                    fn = n.func
                    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    self.assertNotIn(name, {"eval", "exec", "create_all", "drop_all", "text"} if f.name not in ("migrate.py",) else {"eval", "exec"}, f"{f.name}:{name}")
            if f.name != "migrate.py":
                for n in ast.walk(tree):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        names = [a.name for a in n.names] + [getattr(n, "module", None) or ""]
                        self.assertFalse(any(x.endswith("migrate") for x in names), f.name)   # nothing imports the migration

    async def test_other_tables_untouched(self):
        async with self.sf() as s:
            await s.execute(text("CREATE TABLE users (id int primary key, stage text)"))
            await s.execute(text("INSERT INTO users VALUES (1, 'new')"))
            await s.commit()
        await self.mk()
        await self.svc.activate_program("casinox-mx", updated_by="admin")
        async with self.sf() as s:
            self.assertEqual((await s.execute(text("select stage from users"))).all(), [("new",)])


if __name__ == "__main__":
    unittest.main()

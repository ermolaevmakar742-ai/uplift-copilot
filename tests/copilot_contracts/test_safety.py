"""Architecture / safety guard: contracts stay isolated, pure and unused by production."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "copilot_contracts").glob("*.py")) + [
    ROOT / "copilot_evidence" / "__init__.py", ROOT / "copilot_evidence" / "contracts.py"]

ALLOWED_TOP = {"math", "dataclasses", "datetime", "enum", "types", "typing", "copilot_contracts",
               "copilot_evidence"}
FORBIDDEN_NAMES = {"open", "eval", "exec", "compile", "__import__", "input"}


def _imports(path):
    out = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                out.append("." * n.level)
            else:
                out.append(n.module or "")
    return out


class SafetyTests(unittest.TestCase):
    def test_files_exist(self):
        self.assertGreaterEqual(len(FILES), 4)
        for f in FILES:
            self.assertTrue(f.exists(), f)

    def test_import_allowlist(self):
        for f in FILES:
            for m in _imports(f):
                self.assertTrue(m and not m.startswith("."), f"{f.name}: relative import {m!r}")
                self.assertIn(m.split(".")[0], ALLOWED_TOP, f"{f.name} imports {m}")

    def test_explicit_forbidden_deps(self):
        banned = ("main", "telethon", "fastapi", "starlette", "sqlalchemy", "asyncio", "aiohttp",
                  "httpx", "requests", "socket", "urllib", "pickle", "os", "subprocess", "openai",
                  "anthropic", "copilot_decisions", "copilot_shadow", "copilot_external", "scout", "client_state")
        for f in FILES:
            for m in _imports(f):
                self.assertNotIn(m.split(".")[0], banned, f"{f.name} imports {m}")

    def test_no_dangerous_calls(self):
        for f in FILES:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, FORBIDDEN_NAMES, f"{f.name}: {n.func.id}()")
                self.assertFalse(isinstance(n, (ast.AsyncFunctionDef, ast.Await)), f.name)

    def test_production_does_not_reference_new_layer(self):
        prod = [ROOT / "main.py", ROOT / "client_state.py", ROOT / "scout.py", ROOT / "run.py"]
        prod += [p for p in (ROOT / "copilot_decisions").glob("*.py") if p.name != "recorder.py"] + list((ROOT / "copilot_shadow").glob("*.py"))
        prod += list((ROOT / "copilot_external").glob("*.py"))
        for f in prod:
            if f.exists():
                txt = f.read_text(encoding="utf-8")
                self.assertNotIn("copilot_contracts", txt, f.name)
                self.assertNotIn("copilot_evidence", txt, f.name)

    def test_no_db_artifacts(self):
        for f in FILES:
            txt = f.read_text(encoding="utf-8").lower()
            for w in ("create table", "alter table", "insert into", "psycopg", "asyncpg"):
                self.assertNotIn(w, txt, f.name)

    def test_import_pulls_no_heavy_modules(self):
        import subprocess
        import sys
        code = ("import sys, copilot_contracts, copilot_evidence.contracts;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','copilot_decisions',"
                "'copilot_shadow','copilot_external') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()

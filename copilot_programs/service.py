"""Partner program service: the only entry point for reading/changing programs.

Read side is deterministic and never invents data:
  get_program / get_active_program      -> Program | None (an inactive program is never returned as active)
  list_programs(active, country, brand) -> sorted by program_id
  find_active_programs_for_context(country, brand, currency) -> every matching ACTIVE program, sorted
  get_active_program_for_context(...)   -> the program ONLY if exactly one active program matches, else None
                                           (none or ambiguous -> None; the caller must not guess)
Write side validates through the contract, stamps updated_at/updated_by and never changes created_at/program_id.
NOT connected to bot replies or prompts (phase 5B)."""
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from copilot_programs.contract import (EDITABLE, Program, ProgramValidationError, build_program, editable_view)
from copilot_programs.repository import ProgramExists, ProgramNotFound, ProgramRepository

__all__ = ["ProgramService", "ProgramExists", "ProgramNotFound", "ProgramValidationError"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm(v: Any) -> str | None:
    return v.strip().casefold() if isinstance(v, str) and v.strip() else None


class ProgramService:
    def __init__(self, session_factory, clock: Callable[[], datetime] = _utcnow):
        self._repo = ProgramRepository(session_factory)
        self._clock = clock

    # ---- reads
    async def get_program(self, program_id: str) -> Program | None:
        return await self._repo.get(program_id) if isinstance(program_id, str) else None

    async def get_active_program(self, program_id: str) -> Program | None:
        p = await self.get_program(program_id)
        return p if p is not None and p.active else None

    async def list_programs(self, *, active: bool | None = None, country: str | None = None,
                            brand: str | None = None) -> list[Program]:
        items = await self._repo.list(active=active)
        c, b = _norm(country), _norm(brand)
        return [p for p in items if (c is None or c.upper() in p.countries)
                and (b is None or p.brand.casefold() == b)]

    async def find_active_programs_for_context(self, *, country: str | None = None, brand: str | None = None,
                                               currency: str | None = None) -> list[Program]:
        cur = _norm(currency)
        return [p for p in await self.list_programs(active=True, country=country, brand=brand)
                if cur is None or (p.currency or "").casefold() == cur]

    async def get_active_program_for_context(self, *, country: str | None = None, brand: str | None = None,
                                             currency: str | None = None) -> Program | None:
        found = await self.find_active_programs_for_context(country=country, brand=brand, currency=currency)
        return found[0] if len(found) == 1 else None

    # ---- writes
    async def create_program(self, program_id: str, data: Mapping, *, updated_by: str, active: bool = False) -> Program:
        now = self._clock()
        p = build_program(program_id, data if isinstance(data, Mapping) else {}, active=active,
                          updated_by=updated_by, created_at=now, updated_at=now)
        await self._repo.insert(p)
        return p

    async def update_program(self, program_id: str, patch: Mapping, *, updated_by: str) -> Program:
        cur = await self.get_program(program_id)
        if cur is None:
            raise ProgramNotFound(program_id)
        patch = patch if isinstance(patch, Mapping) else {}
        bad = sorted(set(patch) - set(EDITABLE))
        if bad:
            raise ProgramValidationError({"_unknown": f"unknown or read-only fields: {bad}"})
        merged = {**editable_view(cur), **patch}
        p = build_program(program_id, merged, active=cur.active, updated_by=updated_by,
                          created_at=cur.created_at, updated_at=self._clock())
        await self._repo.update(p)
        return p

    async def _set_active(self, program_id: str, active: bool, updated_by: str) -> Program:
        cur = await self.get_program(program_id)
        if cur is None:
            raise ProgramNotFound(program_id)
        p = build_program(program_id, editable_view(cur), active=active, updated_by=updated_by,
                          created_at=cur.created_at, updated_at=self._clock())
        await self._repo.update(p)
        return p

    async def activate_program(self, program_id: str, *, updated_by: str) -> Program:
        return await self._set_active(program_id, True, updated_by)

    async def deactivate_program(self, program_id: str, *, updated_by: str) -> Program:
        return await self._set_active(program_id, False, updated_by)

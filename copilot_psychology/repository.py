"""Append-only storage + bounded reads for psychology assessments."""
import logging
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from copilot_psychology.contracts import PsychologicalAssessment
from copilot_psychology.store import psychology_assessments as t

logger = logging.getLogger("copilot_psychology")


class PsychologyConflict(Exception):
    pass


class PsychologyRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    async def save(self, decision_id: str, chat_id: int, created_at: datetime, a: PsychologicalAssessment) -> bool:
        """True = inserted; False = the same decision already has the identical assessment; PsychologyConflict = a different one."""
        row = dict(assessment_id=a.assessment_id, decision_id=decision_id, chat_id=chat_id, created_at=created_at, knowledge_version=a.knowledge_version,
                   engine_version=a.engine_version, strategy_version=a.strategy_version, psychological_state=a.psychological_state.value,
                   recommended_strategy=a.recommended_strategy.value, payload=a.to_dict())
        async with self._sf() as s:
            try:
                await s.execute(insert(t).values(**row))
                await s.commit()
                return True
            except IntegrityError:
                await s.rollback()
                cur = (await s.execute(select(t.c.assessment_id).where(t.c.decision_id == decision_id))).scalar()
                if cur == a.assessment_id:
                    return False
                raise PsychologyConflict(decision_id)

    async def latest_for_chat(self, chat_id: int):
        async with self._sf() as s:
            r = (await s.execute(select(t).where(t.c.chat_id == chat_id).order_by(t.c.created_at.desc()).limit(1))).mappings().first()
        return None if r is None else {"decision_id": r["decision_id"], "created_at": r["created_at"], "assessment": PsychologicalAssessment.from_dict(r["payload"])}

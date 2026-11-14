"""Read-only data provider for the offline evaluator: SQLAlchemy Core `select()` only, inside `SET TRANSACTION READ ONLY` on
PostgreSQL. Bounded: aggregates (unique leads, totals) are exact SQL counts; row lists are the most recent `max_*` rows
(deterministic order), and `truncated` says when a list is shorter than its table. A missing optional table is reported in
`notes` / `tables`, never invented and never created. Rows that are not valid contracts are COUNTED in `unreadable`."""
import json
from datetime import timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, MetaData, String, Table, func, select, text
from sqlalchemy.dialects.postgresql import JSONB

from copilot_attribution.contract import Interruption
from copilot_attribution.rules_v2 import BoundaryRef
from copilot_outcomes.contracts import Outcome
from copilot_uplift.evaluator import UpliftData

_md = MetaData()                      # private, read definitions only; never used for DDL
_J = JSON().with_variant(JSONB(), "postgresql")
_dec = Table("production_decisions", _md, Column("decision_id", String), Column("chat_id", BigInteger),
             Column("created_at", DateTime(timezone=True)), Column("regime", String), Column("trigger", String), Column("decision_path", String),
             Column("client_state_snapshot", _J))
_rb = Table("response_boundaries", _md, Column("response_boundary_id", String), Column("decision_id", String),
            Column("chat_id", BigInteger), Column("telegram_message_id", BigInteger), Column("sent_at", DateTime(timezone=True)),
            Column("status", String), Column("metadata", _J))
_out = Table("outcomes", _md, Column("outcome_id", String), Column("decision_id", String), Column("chat_id", BigInteger),
             Column("observed_at", DateTime(timezone=True)), Column("outcome_type", String), Column("value", _J),
             Column("confidence", Float(53)), Column("source", String), Column("contract_version", String), Column("metadata", _J))
_act = Table("ai_activity", _md, Column("user_id", BigInteger), Column("event_type", String), Column("created_at", DateTime(timezone=True)))
_asg = Table("uplift_assignments", _md, Column("assignment_id", String), Column("chat_id", BigInteger), Column("arm", String),
             Column("assignment_probability", Float), Column("decided_at", DateTime(timezone=True)), Column("context", String),
             Column("contract_version", String), Column("payload", _J))
INTERRUPTION_EVENTS = {"followup_sent": "FOLLOWUP", "fixed_trigger": "FIXED_TRIGGER", "insult_block": "INSULT_BLOCK"}


def read_only_sql(dialect_name: str) -> str | None:
    return "SET TRANSACTION READ ONLY" if dialect_name == "postgresql" else None


class SqlUpliftProvider:
    def __init__(self, session_factory, *, max_decisions: int = 20000, max_boundaries: int = 100000, max_outcomes: int = 50000,
                 max_assignments: int = 50000, assume_utc_naive: bool = False):
        self._sf, self._md, self._mb, self._mo, self._ma, self._utc = session_factory, max_decisions, max_boundaries, max_outcomes, max_assignments, assume_utc_naive

    def _t(self, v: Any):
        return v.replace(tzinfo=timezone.utc) if (self._utc and v is not None and getattr(v, "tzinfo", 1) is None) else v

    @staticmethod
    def _json(v):
        return json.loads(v) if isinstance(v, str) else v

    async def load(self) -> UpliftData:
        notes, unreadable, tables = [], {"decisions": 0, "boundaries": 0, "outcomes": 0, "interruptions": 0}, {}
        async with self._sf() as s:
            ro = read_only_sql(s.bind.dialect.name if s.bind is not None else "postgresql")
            if ro:
                await s.execute(text(ro))
            leads = (await s.execute(select(func.count(func.distinct(_dec.c.chat_id))))).scalar() or 0
            n_dec = (await s.execute(select(func.count()).select_from(_dec))).scalar() or 0
            drows = (await s.execute(select(_dec).order_by(_dec.c.created_at.desc(), _dec.c.decision_id.desc()).limit(self._md))).mappings().all()
            brows, orows, arows, n_sent, n_out = [], [], [], None, None
            starts = {}                                                    # when each branch started to be recorded as decisions
            for path in ("followup", "fixed_trigger"):
                starts[path] = self._t((await s.execute(select(func.min(_dec.c.created_at)).where(_dec.c.decision_path == path))).scalar())
            try:
                n_sent = (await s.execute(select(func.count()).select_from(_rb).where(_rb.c.status == "SENT"))).scalar() or 0
                brows = (await s.execute(select(_rb).order_by(_rb.c.sent_at.desc(), _rb.c.response_boundary_id.desc()).limit(self._mb))).mappings().all()
                tables["response_boundaries"] = True
            except Exception:
                tables["response_boundaries"] = False
                notes.append("response_boundaries_unavailable: table could not be read")
            try:
                n_out = (await s.execute(select(func.count()).select_from(_out))).scalar() or 0
                orows = (await s.execute(select(_out).order_by(_out.c.observed_at.desc(), _out.c.outcome_id.desc()).limit(self._mo))).mappings().all()
                tables["outcomes"] = True
            except Exception:
                tables["outcomes"] = False
                notes.append("outcomes_unavailable: table could not be read")
            try:
                arows = (await s.execute(select(_act).where(_act.c.event_type.in_(list(INTERRUPTION_EVENTS)))
                                         .where(_act.c.user_id.is_not(None)).order_by(_act.c.created_at))).mappings().all()
            except Exception:
                notes.append("interruptions_unavailable: ai_activity could not be read")
            try:
                n_asg = (await s.execute(select(func.count()).select_from(_asg))).scalar() or 0
                arows_asg = (await s.execute(select(_asg).order_by(_asg.c.decided_at.desc(), _asg.c.assignment_id.desc()).limit(self._ma))).mappings().all()
                tables["uplift_assignments"] = True
            except Exception:
                notes.append("uplift_assignments_unavailable: table could not be read")
                n_asg, arows_asg = None, []
            await s.rollback()
        decisions = []
        for r in drows:
            try:
                created = self._t(r["created_at"])
                if created is None or created.tzinfo is None or not isinstance(r["chat_id"], int):
                    raise ValueError("bad row")
                decisions.append({"decision_id": r["decision_id"], "chat_id": r["chat_id"], "created_at": created,
                                  "regime": r["regime"], "trigger": r["trigger"], "decision_path": r["decision_path"],
                                  "state": self._json(r["client_state_snapshot"])})
            except Exception:
                unreadable["decisions"] += 1
        boundaries = []
        for r in brows:
            try:
                meta = self._json(r["metadata"]) or {}
                boundaries.append(BoundaryRef(r["response_boundary_id"], r["decision_id"], r["chat_id"], self._t(r["sent_at"]),
                                              r["status"], r["status"] == "SENT", meta.get("part_index"), meta.get("parts_total")))
            except Exception:
                unreadable["boundaries"] += 1
        outcomes = []
        for r in orows:
            try:
                t = self._t(r["observed_at"])
                outcomes.append(Outcome.from_dict({
                    "contract_version": r["contract_version"], "outcome_id": r["outcome_id"], "decision_id": r["decision_id"],
                    "chat_id": r["chat_id"], "observed_at": t.isoformat() if t is not None else None, "outcome_type": r["outcome_type"],
                    "value": self._json(r["value"]), "confidence": r["confidence"], "source": r["source"],
                    "metadata": self._json(r["metadata"])}))
            except Exception:
                unreadable["outcomes"] += 1
        ints = []
        # decisions of the interrupting branches are the interruptions themselves (each one never interrupts itself)
        for d in decisions:
            kind = {"followup": "FOLLOWUP", "fixed_trigger": "FIXED_TRIGGER"}.get(d["decision_path"])
            if kind:
                ints.append(Interruption(kind, d["chat_id"], d["created_at"], d["decision_id"]))
        for r in arows:
            try:
                at = self._t(r["created_at"])
                path = {"followup_sent": "followup", "fixed_trigger": "fixed_trigger"}.get(r["event_type"])
                if path and starts.get(path) is not None and at >= starts[path]:
                    continue                                              # from here on the branch is covered by decision records (no duplicate)
                ints.append(Interruption(INTERRUPTION_EVENTS[r["event_type"]], r["user_id"], at))
            except Exception:
                unreadable["interruptions"] += 1
        assignments = []
        for r in arows_asg:
            try:
                if r["arm"] not in ("CONTROL", "TREATMENT"):
                    raise ValueError("bad arm")
                p = r["assignment_probability"]
                if not (isinstance(p, (int, float)) and 0.0 < float(p) < 1.0):
                    raise ValueError("bad probability")
                assignments.append((str(r["assignment_id"]), int(r["chat_id"]), r["arm"], float(p), self._t(r["decided_at"])))
            except Exception:
                unreadable["assignments"] = unreadable.get("assignments", 0) + 1
        truncated = (n_dec > len(drows)) or (n_out is not None and n_out > len(orows)) or (n_asg is not None and n_asg > len(arows_asg)) or (n_sent is not None and n_sent > len(brows)) or (len(brows) >= self._mb)
        return UpliftData(tuple(sorted(decisions, key=lambda d: (d["created_at"], d["decision_id"]))), tuple(boundaries), tuple(outcomes),
                          tuple(ints), int(leads), int(n_dec), n_sent, n_out, bool(truncated), unreadable, tuple(notes), tables,
                          tuple(sorted(assignments)), (int(n_asg) if n_asg is not None else None))

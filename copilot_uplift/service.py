"""Orchestration for the panel/API: load (read-only) -> evaluate -> gates -> activation state. No FastAPI, no main.py.

status() runs a LIVE offline evaluation (bounded read-only queries). Nothing is written by status(). evaluate_and_store() writes
one evaluation snapshot (append-only) if the table exists. enable()/disable() append activation events (never UPDATE).
The kill switch (COPILOT_UPLIFT_ENABLED) is read here only to REPORT; activation can never override it."""
from datetime import datetime, timezone
from typing import Callable, Mapping

from copilot_uplift.activation import (ActivationError, CONFIRMATION, effective_status, influence_allowed, replay, request_disable,
                                    request_enable)
from copilot_uplift.contract import ActivationState as S, UpliftEvaluation
from copilot_uplift.evaluator import evaluate_uplift
from copilot_uplift.gates import SHADOW_FLAG, GateReport, compute_gates, kill_switch_on, shadow_enabled
from copilot_uplift.policy import DEFAULT_POLICY, UpliftPolicy
from copilot_uplift.provider import SqlUpliftProvider
from copilot_uplift.repository import ActivationRepository, EvaluationRepository, ShadowRepository, UpliftCorrupt

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pct(v):
    return None if v is None else round(100 * v, 1)


class UpliftService:
    def __init__(self, session_factory, *, policy: UpliftPolicy = DEFAULT_POLICY, clock: Callable[[], datetime] | None = None,
                 provider=None, env: Mapping | None = None):
        self._sf, self.policy, self._clock = session_factory, policy, clock or _utcnow
        self._provider = provider or SqlUpliftProvider(session_factory)
        self._env = env
        self.activations, self.evaluations, self.shadow = ActivationRepository(session_factory), EvaluationRepository(session_factory), ShadowRepository(session_factory)

    async def _events(self):
        """(events, storage_ok, corrupt). Missing table -> storage not ok (state OFF, activation impossible)."""
        try:
            return await self.activations.list_events(), True, False
        except UpliftCorrupt:
            return (), True, True
        except Exception:
            return (), False, False

    async def evaluate(self) -> UpliftEvaluation:
        data = await self._provider.load()
        return evaluate_uplift(data, self.policy, evaluated_at=self._clock())

    async def evaluate_and_store(self) -> tuple[UpliftEvaluation, bool]:
        e = await self.evaluate()
        try:
            return e, await self.evaluations.save(e) or True
        except Exception:
            return e, False

    async def status(self) -> dict:
        now = self._clock()
        ev = await self.evaluate()
        events, storage_ok, corrupt = await self._events()
        stored = replay(events) if not corrupt else replay([])
        if corrupt:
            from copilot_uplift.activation import StoredState
            stored = StoredState(S.BLOCKED, None, True, "activation_events_unreadable")
        report: GateReport = compute_gates(ev, self.policy, activation_storage_ok=storage_ok and not corrupt)
        status = effective_status(stored, report)
        kill = kill_switch_on(self._env)
        infl = influence_allowed(stored, kill_switch=kill, policy=self.policy, evaluation=ev, now=now)
        try:
            last = await self.evaluations.latest()
        except Exception:
            last = None
        try:
            shadow_n = await self.shadow.count()
            shadow_table = True
        except Exception:
            shadow_n, shadow_table = None, False
        m, c = ev.metrics, ev.coverage
        can_enable = storage_ok and not corrupt and stored.state is not S.ACTIVE and report.all_passed and not report.integrity_failed
        return {
            "status": status.value, "locked": status in (S.OFF, S.BLOCKED), "stored_state": stored.state.value,
            "unique_leads": m["unique_leads"], "leads_required": self.policy.min_unique_leads,
            "decisions": m["decisions"], "outcomes": m["outcomes"], "attributed_outcomes": m["attributed_outcomes"],
            "response_boundaries": m["response_boundaries"], "eligible_samples": m["eligible_samples"],
            "control_samples": m["control_samples"], "treatment_samples": m["treatment_samples"], "actions": m["actions"],
            "control_status": ev.control_status,
            "response_boundary_coverage_pct": _pct(c["response_boundary_coverage"]),
            "response_boundary_coverage_since_capture_pct": _pct(c["response_boundary_coverage_since_capture"]),
            "attribution_coverage_pct": _pct(c["attribution_coverage"]),
            "attribution_status": "READY" if report.to_dict()["gates"][1]["passed"] and ev.integrity["attribution_integrity_errors"] == 0 else "NOT READY",
            "data_quality": ev.data_quality.value, "policy": self.policy.policy_version, "estimator": ev.estimator_version,
            "feature_version": ev.feature_version, "dataset_version": ev.dataset_version,
            "evaluated_at": ev.evaluation_timestamp.isoformat(),
            "last_stored_evaluation": last.evaluation_timestamp.isoformat() if last else None,
            "conversion_by_action": [x.to_dict() for x in ev.conversion_by_action],
            "uplift_estimates": [x.to_dict() for x in ev.uplift_estimates],
            "missing_signals": dict(ev.missing_signals), "integrity": dict(ev.integrity), "truncated": ev.truncated,
            "gates": [g for g in report.to_dict()["gates"]], "missing_gates": list(report.missing),
            "enable_button_enabled": bool(can_enable), "disable_button_enabled": bool(storage_ok and not corrupt and stored.state is S.ACTIVE),
            "kill_switch": kill, "influence_possible": infl.allowed, "influence_reason": infl.reason,
            "shadow": {"flag": shadow_enabled(self._env), "table_ready": shadow_table, "records": shadow_n},
            "activation_storage": "ready" if storage_ok and not corrupt else ("corrupt" if corrupt else "not_migrated"),
            "confirm_phrase": CONFIRMATION,
        }

    async def enable(self, changed_by: str, confirmation: str) -> dict:
        events, storage_ok, corrupt = await self._events()
        if not storage_ok:
            raise ActivationError("activation_storage_unavailable")
        if corrupt:
            raise ActivationError("activation_state_corrupt")
        ev = await self.evaluate()
        report = compute_gates(ev, self.policy, activation_storage_ok=True)
        event, outcome = request_enable(events, changed_by=changed_by, confirmation=confirmation, report=report,
                                        policy=self.policy, now=self._clock())
        await self.activations.append(event)
        return {"state": outcome.value, "event": event.to_dict(), "missing_gates": list(report.missing)}

    async def disable(self, changed_by: str, reason: str = "disabled by admin") -> dict:
        events, storage_ok, corrupt = await self._events()
        if not storage_ok:
            raise ActivationError("activation_storage_unavailable")
        if corrupt:
            raise ActivationError("activation_state_corrupt")
        ev = await self.evaluate()
        report = compute_gates(ev, self.policy, activation_storage_ok=True)
        event = request_disable(events, changed_by=changed_by, reason=reason, policy=self.policy, now=self._clock(),
                                integrity_failure=report.integrity_failed)
        await self.activations.append(event)
        return {"state": event.new_state.value, "event": event.to_dict()}

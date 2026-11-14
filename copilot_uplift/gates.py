"""Activation gates + kill switch. Pure. EVERY gate is mandatory: one FAIL keeps production influence closed.

Gates (all derived from the offline evaluation, never trusted from elsewhere):
  unique_leads            unique chat_id >= policy.min_unique_leads (500). A lead is a unique chat_id, not a message/decision/outcome.
                          500 is a minimum, NOT evidence of quality: it never activates anything by itself.
  attribution_v2_operational   response boundaries exist and cover at least one decision
  response_boundary_integrity  boundaries exist and none violates (sent after its decision, same chat)
  outcome_pipeline_operational at least one outcome stored
  eligible_outcomes_nonzero    at least one outcome is ATTRIBUTED
  action_variation        at least two distinct REGISTERED actions observed among eligible samples (counted from the data, not the registry)
  offline_evaluator       the evaluator ran and the data quality is above INSUFFICIENT_DATA
  no_data_leakage         zero leakage violations
  no_attribution_integrity_errors  zero attribution integrity errors
  policy_locked           the policy is locked, fingerprint-verified and the evaluation was made with it
  activation_storage      the append-only activation event store is available
The kill switch COPILOT_UPLIFT_ENABLED (default false) is independent: it can only ever REMOVE influence."""
import os
from dataclasses import dataclass
from typing import Mapping

from copilot_uplift.actions import DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import DataQuality, UpliftEvaluation
from copilot_uplift.policy import UpliftPolicy, is_locked

KILL_SWITCH_ENV = "COPILOT_UPLIFT_ENABLED"
SHADOW_FLAG = "COPILOT_UPLIFT_SHADOW_ENABLED"


def shadow_enabled(env: Mapping | None = None) -> bool:
    v = (os.environ if env is None else env).get(SHADOW_FLAG, "false")
    return isinstance(v, str) and v.strip().lower() in {"1", "true", "yes", "on"}


def kill_switch_on(env: Mapping | None = None) -> bool:
    v = (os.environ if env is None else env).get(KILL_SWITCH_ENV, "false")
    return isinstance(v, str) and v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    gates: tuple[Gate, ...]
    integrity_failed: bool

    @property
    def all_passed(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(g.name for g in self.gates if not g.passed)

    def to_dict(self) -> dict:
        return {"all_passed": self.all_passed, "integrity_failed": self.integrity_failed, "missing": list(self.missing),
                "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail} for g in self.gates]}


def observed_actions(evaluation: UpliftEvaluation, registry: ActionRegistry = DEFAULT_REGISTRY) -> tuple[str, ...]:
    """Distinct REGISTERED actions that have at least one eligible sample in the data: what was really observed, never the registry size."""
    return tuple(sorted({s.action_id for s in evaluation.conversion_by_action if s.eligible > 0 and s.action_id in registry}))


def compute_gates(evaluation: UpliftEvaluation | None, policy: UpliftPolicy, *, activation_storage_ok: bool,
                  registry: ActionRegistry = DEFAULT_REGISTRY) -> GateReport:
    if not isinstance(evaluation, UpliftEvaluation):
        return GateReport(tuple(Gate(n, False, "no evaluation") for n in (
            "unique_leads", "attribution_v2_operational", "response_boundary_integrity", "outcome_pipeline_operational",
            "eligible_outcomes_nonzero", "action_variation", "offline_evaluator", "no_data_leakage",
            "no_attribution_integrity_errors", "provider_data_complete", "policy_locked", "activation_storage")), False)
    m, i, c = evaluation.metrics, evaluation.integrity, evaluation.coverage
    leads, need = m["unique_leads"], policy.min_unique_leads
    seen = observed_actions(evaluation, registry)
    gates = (
        Gate("unique_leads", leads >= need, f"{leads} / {need} unique leads"),
        Gate("attribution_v2_operational", m["response_boundaries"] >= 1 and bool(c["response_boundary_coverage"]),
             f"{m['response_boundaries']} sent boundaries, coverage {c['response_boundary_coverage']}"),
        Gate("response_boundary_integrity", m["response_boundaries"] >= 1 and i["boundary_violations"] == 0,
             f"{m['response_boundaries']} boundaries, {i['boundary_violations']} violations"),
        Gate("outcome_pipeline_operational", m["outcomes"] >= 1, f"{m['outcomes']} outcomes"),
        Gate("eligible_outcomes_nonzero", m["attributed_outcomes"] >= 1, f"{m['attributed_outcomes']} attributed outcomes"),
        Gate("action_variation", len(seen) >= 2 and m["actions"] == len(seen),
             f"{len(seen)} observed mapped actions with eligible samples: {', '.join(seen) or 'none'} (registry size is irrelevant)"),
        Gate("offline_evaluator", evaluation.data_quality.rank >= DataQuality(policy.min_quality_production).rank,
             f"data quality {evaluation.data_quality.value}; production requires {policy.min_quality_production}"),
        Gate("no_data_leakage", i["leakage_violations"] == 0, f"{i['leakage_violations']} leakage violations"),
        Gate("no_attribution_integrity_errors", i["attribution_integrity_errors"] == 0,
             f"{i['attribution_integrity_errors']} attribution integrity errors"),
        Gate("provider_data_complete", not evaluation.truncated
             and not any(v for k, v in evaluation.missing_signals.items()
                         if k.startswith("provider_unreadable_rows:")
                         or k.startswith("provider_required_table_unavailable:")
                         or k.startswith("experiment_observation_unknown:")
                         or k in {"ambiguous_experiment_assignments", "invalid_experiment_assignment_rows"}),
             "complete provider data" if (not evaluation.truncated and not any(
                 v for k, v in evaluation.missing_signals.items()
                 if k.startswith("provider_unreadable_rows:")
                 or k.startswith("provider_required_table_unavailable:")
                 or k.startswith("experiment_observation_unknown:")
                 or k in {"ambiguous_experiment_assignments", "invalid_experiment_assignment_rows"})) else "truncated, unreadable, unavailable or ambiguous provider data"),
        Gate("policy_locked", is_locked(policy) and evaluation.policy_version == policy.policy_version,
             f"policy {policy.policy_version} {'locked' if is_locked(policy) else 'NOT locked'}"),
        Gate("activation_storage", bool(activation_storage_ok), "available" if activation_storage_ok else "not migrated / unavailable"),
    )
    failed = (i["leakage_violations"] > 0 or i["attribution_integrity_errors"] > 0 or i["boundary_violations"] > 0
              or evaluation.truncated or any(v for k, v in evaluation.missing_signals.items()
                  if k.startswith("provider_unreadable_rows:")
                  or k.startswith("provider_required_table_unavailable:")
                  or k.startswith("experiment_observation_unknown:")
                  or k in {"ambiguous_experiment_assignments", "invalid_experiment_assignment_rows"}))
    return GateReport(gates, failed)

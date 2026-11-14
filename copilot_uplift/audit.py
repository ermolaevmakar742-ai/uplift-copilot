"""Offline coverage audit: how observable COPILOT is today and what a future Uplift evaluation could use. Pure (no SQL, no clock, no network,
no LLM). It reports facts and statuses only: no ranking, no recommendation, no causal claim.

Inputs: the UpliftData the read-only provider loaded, its evaluation, and (optionally) the flow-experiment configuration."""
from collections import Counter
from typing import Mapping

from copilot_attribution.contract import AttributionPolicy, Relationship
from copilot_attribution.rules import DEFAULT_POLICY as ATTRIBUTION_POLICY
from copilot_attribution.rules_v2 import BoundaryRef, attribute_all_v2, boundary_info
from copilot_attribution.contract import DecisionRef
from copilot_uplift import action_mapping as AM
from copilot_uplift.actions import DEFAULT_REGISTRY
from copilot_uplift.contract import DataQuality, UpliftEvaluation
from copilot_uplift.evaluator import UpliftData
from copilot_uplift.experiments import audit_flow_experiment
from copilot_uplift.gates import observed_actions


def _pct(n: int, d: int):
    return None if d == 0 else round(100 * n / d, 1)


def coverage_audit(data: UpliftData, evaluation: UpliftEvaluation, flow_versions: Mapping | None = None,
                   attribution_policy: AttributionPolicy | None = None) -> dict:
    decisions = list(data.decisions)
    refs = [DecisionRef(d["decision_id"], d["chat_id"], d["created_at"]) for d in decisions]
    boundaries = [BoundaryRef.of(b) for b in data.boundaries]
    results = attribute_all_v2(list(data.outcomes), refs, boundaries, interruptions=list(data.interruptions),
                               policy=attribution_policy or ATTRIBUTION_POLICY)
    bmap: dict[str, list] = {}
    for b in boundaries:
        if b.decision_id:
            bmap.setdefault(b.decision_id, []).append(b)
    with_boundary = [d for d in decisions if boundary_info(DecisionRef(d["decision_id"], d["chat_id"], d["created_at"]), bmap.get(d["decision_id"], ())).end is not None]
    attributed_to = {r.decision_id for r in results if r.relationship is Relationship.ATTRIBUTED}
    by_path = Counter((d.get("decision_path") or "llm_reply") for d in decisions)
    mapped_rules = [r for r in AM.RULES_V2 if r.status is AM.MappingStatus.MAPPED]
    sample_capable = sorted({r.action_id for r in mapped_rules if r.sample_capable})
    observed = observed_actions(evaluation)
    exp = audit_flow_experiment(flow_versions) if flow_versions is not None else None
    reasons = []
    if evaluation.control_status == "CONTROL_UNAVAILABLE":
        reasons.append("control_unavailable: no randomized control exists")
    if exp is not None and exp.status != "DIFFERENT_ARMS_UNRECORDED_ASSIGNMENT":
        reasons.append(f"flow_experiment_{exp.status.lower()}: " + exp.reason)
    elif exp is not None:
        reasons.append("flow_experiment_assignment_not_recorded_as_randomization")
    if evaluation.data_quality is not DataQuality.CAUSAL_READY:
        reasons.append(f"data_quality_{evaluation.data_quality.value.lower()}")
    return {
        "decision_coverage": {"total": len(decisions), "by_decision_path": dict(sorted(by_path.items())),
                              "with_client_state_snapshot": sum(1 for d in decisions if d.get("state")),
                              "branch_decisions_recorded": sum(v for k, v in by_path.items() if k != "llm_reply")},
        "mapping": {"version": AM.MAPPING_VERSION, "rules_fingerprint": AM.rules_fingerprint(), "rules": AM.summary(),
                    "sample_capable_actions": sample_capable,
                    "mapped_actions_incl_event_level": sorted({r.action_id for r in mapped_rules}),
                    "registry_actions_never_mapped": sorted(set(DEFAULT_REGISTRY.ids()) - {r.action_id for r in mapped_rules})},
        "observed_mapped_actions": list(observed), "eligible_samples": evaluation.metrics["eligible_samples"],
        "boundary_coverage": {"decisions_with_sent_boundary": len(with_boundary), "pct": _pct(len(with_boundary), len(decisions)),
                              "pct_since_capture": evaluation.coverage["response_boundary_coverage_since_capture"] and round(100 * evaluation.coverage["response_boundary_coverage_since_capture"], 1)},
        "attribution_coverage": {"outcomes": len(results), "attributed_outcomes": sum(1 for r in results if r.relationship is Relationship.ATTRIBUTED),
                                 "decisions_with_attributable_outcomes": len(attributed_to), "pct_outcomes": _pct(sum(1 for r in results if r.relationship is Relationship.ATTRIBUTED), len(results))},
        "action_variation": {"status": "PASS" if len(observed) >= 2 else "FAIL", "observed": list(observed), "needed": 2},
        "causal_readiness": {"status": "CAUSAL_READY" if evaluation.data_quality is DataQuality.CAUSAL_READY else "NOT_CAUSAL_READY",
                             "data_quality": evaluation.data_quality.value, "control_status": evaluation.control_status,
                             "experiment": exp.status if exp else None, "reasons": reasons},
        "missing_signals": dict(evaluation.missing_signals),
    }

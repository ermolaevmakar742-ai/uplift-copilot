"""Audit of the existing flow experiment (v37 / v38). Pure: takes the flow-config mapping, never imports main, never creates randomness.

The experiment assigns users by a hash of (experiment name, user id) % traffic percent. That is an assignment mechanism, but an experiment
can identify an effect only if its arms DIFFER in behaviour. If every arm has the same prompt suffix the arms are identical:
status IDENTICAL_ARMS -> NON_CAUSAL, control CONTROL_UNAVAILABLE. Even with different arms the assignment must be RECORDED as randomization
(with probabilities) before any causal reading: until then DIFFERENT_ARMS_UNRECORDED_ASSIGNMENT, still not causal. Nothing here makes an
experiment pass a gate."""
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ExperimentAudit:
    status: str                    # IDENTICAL_ARMS | DIFFERENT_ARMS_UNRECORDED_ASSIGNMENT | NO_ARMS
    causal: bool
    control: str                   # CONTROL_UNAVAILABLE
    arms: tuple[str, ...]
    distinct_behaviours: int
    reason: str


def audit_flow_experiment(flow_versions: Mapping[str, Mapping]) -> ExperimentAudit:
    arms = tuple(sorted(flow_versions)) if isinstance(flow_versions, Mapping) else ()
    if len(arms) < 2:
        return ExperimentAudit("NO_ARMS", False, "CONTROL_UNAVAILABLE", arms, len(arms), "fewer than two arms")
    behaviours = {str((flow_versions[a] or {}).get("prompt_suffix", "")) for a in arms}
    if len(behaviours) == 1:
        return ExperimentAudit("IDENTICAL_ARMS", False, "CONTROL_UNAVAILABLE", arms, 1,
                               "all arms have the same prompt suffix: treatment and control do not differ; NON_CAUSAL")
    return ExperimentAudit("DIFFERENT_ARMS_UNRECORDED_ASSIGNMENT", False, "CONTROL_UNAVAILABLE", arms, len(behaviours),
                           "arms differ but assignment probabilities are not recorded: not usable as a randomized control")

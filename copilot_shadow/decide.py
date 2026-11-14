"""Transparent, deterministic PRE-UPLIFT shadow decision.

This is NOT an uplift model and NOT an autonomous engine. It is an ordered
rule list over the snapshot. WAIT is always reported as a separate baseline;
no incremental effect vs WAIT is estimated (uplift_status="not_estimated").
"""
from copilot_shadow.actions import ALL_ACTIONS, ShadowAction as A

MODEL_NAME = "rule_based_pre_uplift"
MODEL_VERSION = "1"
DECISION_STATUS = "pre_uplift"

_POST_CONVERSION = {"deposited", "converted"}
_OBJECTION_STAGES = {"objection", "hesitation", "blocked"}
_OFFER_STAGES = {"offer", "action"}
_OFFER_READY_STAGES = {"qualified", "conversation"}


def _eligibility(s: dict) -> dict[A, tuple[bool, str]]:
    stage = s.get("stage") or "new"
    intent = s.get("intent")
    risk = s.get("risk") or {}
    post = stage in _POST_CONVERSION
    e: dict[A, tuple[bool, str]] = {}
    e[A.HUMAN_HANDOFF] = (
        bool(risk.get("self_exclusion") or risk.get("minor") or s.get("manual_mode")),
        "risk flag or manual_mode",
    )
    e[A.WAIT] = (bool(s.get("blocked")), "user blocked")
    e[A.HANDLE_OBJECTION] = (
        not post and (intent == "objection" or stage in _OBJECTION_STAGES),
        "objection intent/stage (not post-conversion)",
    )
    e[A.PRESENT_OFFER] = (
        not post and (stage in _OFFER_STAGES
                      or (intent == "ready" and stage in _OFFER_READY_STAGES)),
        "offer/action stage, or ready intent in qualified/conversation",
    )
    e[A.CONTINUE_DIALOGUE] = (True, "default")
    return e


# Priority: first eligible wins. WAIT outranks only the active moves.
_PRIORITY = (A.HUMAN_HANDOFF, A.WAIT, A.HANDLE_OBJECTION, A.PRESENT_OFFER,
             A.CONTINUE_DIALOGUE)


def decide(snapshot: dict) -> dict:
    elig = _eligibility(snapshot)
    chosen = next(a for a in _PRIORITY if elig[a][0])
    return {
        "chosen_action": chosen.value,
        "chosen_rule": elig[chosen][1],
        "candidate_actions": [
            {"action": a.value, "eligible": elig[a][0], "rule": elig[a][1]}
            for a in ALL_ACTIONS
        ],
        "wait_baseline": {
            "action": A.WAIT.value,
            "uplift_vs_wait": None,
            "uplift_status": "not_estimated",
            "note": "no causal uplift exists; WAIT recorded as baseline only",
        },
        "decision_status": DECISION_STATUS,
        "shadow_mode": True,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
    }

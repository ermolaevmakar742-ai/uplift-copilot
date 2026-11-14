"""Shadow Store v2 research fields for ONE decision (pure + an optional hypothetical bandit recommendation).

Returns plain column values for the shadow row. Never touches the production action / reply. Action vocabulary is the EXISTING
registry (copilot_uplift.actions.DEFAULT_REGISTRY, actions.v1). The production action is made canonical ONLY by the existing
action_mapping.map_decision v2 over the decision's ledger metadata (map_production); a routing regime is a label, not an action,
so casual / agent / retention / hard are UNMAPPED there and stay UNMAPPED here."""
import logging

from copilot_bandit import contract as C
from copilot_bandit.backend import LinUCBBackend
from copilot_bandit.features import DIM, encode, feature_vector, features_ref
from copilot_uplift.action_mapping import MAPPING_VERSION, MappingStatus, map_decision
from copilot_uplift.actions import BASELINE_ACTION_ID, DEFAULT_REGISTRY
from copilot_uplift.features import FEATURE_VERSION
from copilot_uplift.policy import DEFAULT_POLICY

logger = logging.getLogger("copilot_shadow")
_backend: LinUCBBackend | None = None


def get_backend() -> LinUCBBackend:
    global _backend
    if _backend is None:
        _backend = LinUCBBackend(DEFAULT_REGISTRY.ids(), DIM, baseline=BASELINE_ACTION_ID,
                                 min_updates=DEFAULT_POLICY.min_samples_per_arm, reward_range=(-1.0, 1.0))   # reward.v1 range
    return _backend


def available_actions(risk=None, manual_mode=False) -> tuple:
    """(registry ids narrowed by safety constraints, restricted?)."""
    risk = risk or {}
    if risk.get("self_exclusion") or risk.get("minor") or manual_mode:
        return ["HUMAN_HANDOFF"], True
    return list(DEFAULT_REGISTRY.ids()), False


def map_production(ledger_meta) -> dict:
    """THE one production-action -> canonical-action mapping (live path AND replay): existing action_mapping.map_decision v2
    over the decision's LEDGER metadata (regime / trigger / decision_path). MAPPED -> an actions.v1 id; AMBIGUOUS / UNMAPPED
    -> NULL, never a guess. Missing metadata -> UNMAPPED + INSUFFICIENT_METADATA. The regime is a label, never an action."""
    version = MAPPING_VERSION
    if not isinstance(ledger_meta, dict) or not ledger_meta.get("decision_path"):
        return {"canonical_action": None, "action_mapping_status": "UNMAPPED", "action_mapping_version": version,
                "action_mapping_reason": "ledger_metadata_missing", "flags": [C.PRODUCTION_ACTION_UNMAPPED, C.INSUFFICIENT_METADATA]}
    res = map_decision(ledger_meta.get("regime"), ledger_meta.get("trigger"), decision_path=ledger_meta.get("decision_path"))
    flags = []
    action = res.action_id if res.status is MappingStatus.MAPPED else None
    if action is not None and action not in DEFAULT_REGISTRY:       # defensive: canonical ids come from actions.v1 only
        action, res = None, None
    if action is None:
        flags.append(C.PRODUCTION_ACTION_AMBIGUOUS if (res is not None and res.status is MappingStatus.AMBIGUOUS)
                     else C.PRODUCTION_ACTION_UNMAPPED)
    elif not res.sample_capable:
        flags.append(C.ACTION_NOT_SAMPLE_CAPABLE)
    return {"canonical_action": action,
            "action_mapping_status": res.status.value if res is not None else "UNMAPPED",
            "action_mapping_version": version,
            "action_mapping_reason": (res.reason if res is not None else "action_not_in_registry")[:64], "flags": flags}


def v2_fields(*, decision_id, chat_id, snapshot, ledger_meta=None, risk=None, manual_mode=False,
              backend: LinUCBBackend | None = None) -> dict:
    """Never raises. Bandit keys appear only when COPILOT_BANDIT_SHADOW_ENABLED is on. `ledger_meta` = the decision's ledger
    row (regime / trigger / decision_path) or None."""
    ids, restricted = available_actions(risk, manual_mode)
    flags = [C.PROPENSITY_UNKNOWN]
    if restricted:
        flags.append(C.SAFETY_RESTRICTED)
    m = map_production(ledger_meta)
    flags += m.pop("flags")
    out = {"feature_version": FEATURE_VERSION, "features_ref": None,
           "available_actions": {"registry_version": DEFAULT_REGISTRY.version, "ids": ids},
           "production_policy_version": C.PRODUCTION_POLICY_VERSION,
           "production_propensity": None, "production_propensity_source": "unknown",   # production gives none: never estimated
           "observation_window": C.observation_window(), "eligibility_flags": flags, **m}
    try:
        fv = feature_vector(decision_id, chat_id, snapshot)
        out["features_ref"] = features_ref(fv)
        if C.bandit_shadow_enabled():
            rec = (backend or get_backend()).recommend(encode(fv), ids, decision_id)
            if rec["status"] == "ok":
                out.update(bandit_action=rec["action"], bandit_policy_version=rec["policy_version"],
                           bandit_propensity=rec["propensity"])
                if rec["cold_start"]:
                    flags.append(C.INSUFFICIENT_DATA)
    except Exception:
        logger.exception("SHADOW research v2 fields failed decision=%s", decision_id)
    return out

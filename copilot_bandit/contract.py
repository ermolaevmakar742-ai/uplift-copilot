"""Research contract: flags, propensity semantics, eligibility vocabulary. Pure, no I/O.

production_propensity  = probability the PRODUCTION policy assigned to the action it took. Production routing is
                         deterministic and records no probability, so it is NULL / source 'unknown'. Never estimated,
                         never replaced by 0/1, never by a surrogate.
bandit_propensity      = probability the research BANDIT policy assigns to its OWN recommendation (LinUCB is a
                         deterministic argmax -> 1.0). A different quantity; never mixed with the production one.
"""
import os

CONTRACT_VERSION = "research-contract-1"
PRODUCTION_POLICY_VERSION = "production-routing-unversioned"   # production has no versioned policy id (deterministic routing)
PROPENSITY_SOURCES = ("known", "unknown", "surrogate")
OBSERVATION_WINDOW_ENV = "COPILOT_SHADOW_OUTCOME_WINDOW_SECONDS"   # same window the Outcome Worker uses
DEFAULT_OBSERVATION_WINDOW = 86400

# eligibility flags (data-quality / OPE-readiness vocabulary; no OPE result is produced here)
PROPENSITY_UNKNOWN = "PROPENSITY_UNKNOWN"
PRODUCTION_ACTION_UNMAPPED = "PRODUCTION_ACTION_UNMAPPED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"        # bandit still below the minimum observations per action
SAFETY_RESTRICTED = "SAFETY_RESTRICTED"        # risk / manual mode: available actions narrowed
PRODUCTION_ACTION_AMBIGUOUS = "PRODUCTION_ACTION_AMBIGUOUS"   # action mapping v2: several registry actions fit, none chosen
INSUFFICIENT_METADATA = "INSUFFICIENT_METADATA"               # ledger metadata for the decision could not be joined
ACTION_NOT_SAMPLE_CAPABLE = "ACTION_NOT_SAMPLE_CAPABLE"       # mapped, but a different mechanism than the arm's sample behaviour
NO_OVERLAP = "NO_OVERLAP"                      # reserved for the future OPE step (needs the whole dataset)
OUTCOME_INCOMPLETE = "OUTCOME_INCOMPLETE"      # reserved for the future OPE step (derived from outcome_status)
ALL_FLAGS = (PROPENSITY_UNKNOWN, PRODUCTION_ACTION_UNMAPPED, PRODUCTION_ACTION_AMBIGUOUS, INSUFFICIENT_METADATA,
             ACTION_NOT_SAMPLE_CAPABLE, INSUFFICIENT_DATA, SAFETY_RESTRICTED, NO_OVERLAP,
             OUTCOME_INCOMPLETE)


def _on(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def bandit_shadow_enabled() -> bool:
    return _on("COPILOT_BANDIT_SHADOW_ENABLED")


def bandit_enabled() -> bool:
    """Kill-switch for any production influence. Nothing reads it for decisions today; default and only safe value: off."""
    return _on("COPILOT_BANDIT_ENABLED")


def ope_enabled() -> bool:
    return _on("COPILOT_OPE_ENABLED")


def observation_window() -> int:
    try:
        v = int(float(os.getenv(OBSERVATION_WINDOW_ENV, "")))
        return v if v > 0 else DEFAULT_OBSERVATION_WINDOW
    except ValueError:
        return DEFAULT_OBSERVATION_WINDOW


def validate_propensity(value, source) -> tuple:
    """(value, source) normalised. 'unknown' carries NO value; 'known'/'surrogate' need a probability in (0, 1]."""
    if source not in PROPENSITY_SOURCES:
        raise ValueError("propensity source must be known|unknown|surrogate")
    if source == "unknown":
        if value is not None:
            raise ValueError("unknown propensity must have no value")
        return None, "unknown"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 1:
        raise ValueError("propensity must be in (0, 1]")
    return float(value), source


def ope_usable(row) -> bool:
    """IPS/SNIPS/DR may use a row's production propensity only when it is KNOWN. surrogate/unknown never qualify."""
    v, s = row.get("production_propensity"), row.get("production_propensity_source")
    try:
        return validate_propensity(v, s)[1] == "known"
    except ValueError:
        return False


def comparison_eligible(row) -> bool:
    """production vs bandit comparison is allowed ONLY on canonical actions: mapping status MAPPED (canonical_action is an
    actions.v1 id) and a bandit recommendation present. The raw `production_action` label is never compared."""
    return (row.get("action_mapping_status") == "MAPPED" and bool(row.get("canonical_action"))
            and bool(row.get("bandit_action")))


def action_ope_eligible(row) -> bool:
    """UNMAPPED / AMBIGUOUS are never OPE-eligible; MAPPED is eligible only if its behaviour is sample-capable."""
    return (row.get("action_mapping_status") == "MAPPED" and bool(row.get("canonical_action"))
            and ACTION_NOT_SAMPLE_CAPABLE not in (row.get("eligibility_flags") or ()))


def ope_eligible(row) -> bool:
    """Necessary (not sufficient) conditions for a row to enter a future OPE: mapped action AND a KNOWN production propensity."""
    return action_ope_eligible(row) and ope_usable(row)

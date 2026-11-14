"""Versioned mapping layer: what production REALLY did -> a registry action. Pure, deterministic, no LLM / network / randomness /
production imports, no message text. Every rule cites the production code that justifies it (`evidence`).

Three outcomes, never a guess:
  MAPPED     one registry action, deterministic and defensible
  AMBIGUOUS  several registry actions fit; the candidates are listed; NO action is chosen
  UNMAPPED   not enough information to name an action (reason given)

AUDIT FINDINGS (production code, 2026-10-02) that drive the rules:
 * `regime` (casual / hard / agent / retention) selects NO distinct behaviour: SYSTEM_PROMPTS holds only the casual prompt (every other
   regime falls back to it) and MODELS maps every customer-facing regime to the same model; both flow versions have an empty
   prompt_suffix. A regime is a routing LABEL of the client's state (objection stage/markers, offer stage, post-conversion), not an
   action. action-mapping.v1 (hard -> ADDRESS_OBJECTION) treated that label as an action and is kept ONLY to reproduce old evaluations.
 * The only per-message prompt variation recorded in a ledger decision is the non-fixed `trigger` suffix: `safety` (answer a safety
   question calmly, no guarantees, stress control over deposit/withdrawal) and `other_platform` (ask how it is going, mention people combine
   platforms, do not insist).
 * Not reproducible from a decision record (so UNMAPPED, not guessed): the onboarding prompt (driven by clients.msg_count, a counter that is
   not stored in the decision), the time-of-day suffix (tone only), memory / OSINT / state blocks (context).
 * Other real behaviours were NOT ledger decisions (follow-up worker, fixed-trigger replies, insult block, injection reply, analyst A3,
   manual mode): only timestamped events. Since phase 5M the follow-up and fixed-trigger branches ALSO write a decision record
   (`decision_path` = "followup" | "fixed_trigger"); NEW traffic only, never back-filled. Those ledger rules below are what can become
   samples. The EVENT-level rules remain for the historical events (never samples). Insult / injection / manual / analyst: unmapped.
 * One action = one sample behaviour: the fixed fraud reply is deterministically a trust response, but BUILD_TRUST's sample slot already
   belongs to the LLM safety reply, so the fraud branch is MAPPED yet NOT sample-capable (a different mechanism must not share an arm).
Two different behaviours are never given the same action as a sample: validate_rules() rejects an action that has two sample-capable behaviours."""
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from copilot_uplift.actions import DEFAULT_REGISTRY, ActionRegistry

MAPPING_VERSION_V1 = "action-mapping.v1"       # legacy, indefensible: kept only for reproducibility of old evaluations
MAPPING_VERSION = "action-mapping.v2"
SUPPORTED_VERSIONS = (MAPPING_VERSION_V1, MAPPING_VERSION)

LEDGER, EVENT = "ledger_decision", "event"


class MappingStatus(str, Enum):
    MAPPED = "MAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMAPPED = "UNMAPPED"


@dataclass(frozen=True)
class MappingResult:
    status: MappingStatus
    action_id: str | None
    behaviour_id: str | None
    candidates: tuple[str, ...]
    reason: str
    unit: str
    sample_capable: bool

    def __post_init__(self):
        if (self.status is MappingStatus.MAPPED) != (self.action_id is not None):
            raise ValueError("action_id exactly when MAPPED")
        if (self.status is MappingStatus.AMBIGUOUS) != (len(self.candidates) >= 2):
            raise ValueError(">=2 candidates exactly when AMBIGUOUS")
        if self.status is MappingStatus.MAPPED and self.behaviour_id is None:
            raise ValueError("a mapped action needs a behaviour id")


@dataclass(frozen=True)
class Rule:
    unit: str
    key: str
    status: MappingStatus
    action_id: str | None
    behaviour_id: str | None
    candidates: tuple[str, ...]
    reason: str
    evidence: str
    sample_capable: bool

    def result(self) -> MappingResult:
        return MappingResult(self.status, self.action_id, self.behaviour_id, self.candidates, self.reason, self.unit, self.sample_capable)


def _m(unit, key, action, behaviour, evidence, reason="deterministic", sample=True):
    return Rule(unit, key, MappingStatus.MAPPED, action, behaviour, (), reason, evidence, sample)


def _a(unit, key, cands, reason, evidence):
    return Rule(unit, key, MappingStatus.AMBIGUOUS, None, None, tuple(cands), reason, evidence, False)


def _u(unit, key, reason, evidence=""):
    return Rule(unit, key, MappingStatus.UNMAPPED, None, None, (), reason, evidence, False)


_NO_DISTINCT = "regime_selects_no_distinct_behaviour"   # label of the client's state, not an action (evidence: same prompt, same model)
RULES_V2: tuple[Rule, ...] = (
    # ---- ledger decisions (llm_reply path): trigger first, it is the only recorded prompt variation
    _m(LEDGER, "trigger:safety", "BUILD_TRUST", "llm_reply_safety_suffix", "main._TRIGGER_SUFFIXES['safety']: answer a safety question calmly, no guarantees"),
    _a(LEDGER, "trigger:other_platform", ("ASK_QUESTION", "ADDRESS_OBJECTION"),
       "ambiguous_ask_or_objection", "main._TRIGGER_SUFFIXES['other_platform']"),
    _u(LEDGER, "regime:hard", _NO_DISTINCT, "main.SYSTEM_PROMPTS (casual only), main.MODELS (same model for all)"),
    _u(LEDGER, "regime:agent", _NO_DISTINCT, "same"),
    _u(LEDGER, "regime:casual", _NO_DISTINCT, "same"),
    _u(LEDGER, "regime:retention", _NO_DISTINCT, "same"),
    # ---- branch decisions (phase 5M): decision_path "followup" / "fixed_trigger"
    Rule(LEDGER, "path:followup", MappingStatus.MAPPED, "FOLLOW_UP", "followup_worker_fu", (), "deterministic",
         "main._followup_worker_tick: FU1-FU3 after user silence (one behaviour family; the stage is kept in branch metadata)", True),
    Rule(LEDGER, "path:followup_holdout", MappingStatus.MAPPED, "DO_NOTHING", "followup_holdout_silence", (), "deterministic",
         "main._followup_worker_tick holdout gate: the randomized CONTROL arm records the skipped proactive follow-up as silence", True),
    Rule(LEDGER, "fixed_trigger:fraud", MappingStatus.MAPPED, "BUILD_TRUST", "fixed_fraud_license", (),
         "mapped_not_sample_capable:second_behaviour_for_same_action", "main.on_message fixed fraud branch sends the licence document", False),
    _a(LEDGER, "fixed_trigger:how_much", ("BUILD_TRUST", "ADDRESS_OBJECTION"), "ambiguous_trust_or_objection_expectation",
       "main._FIXED_TRIGGER_RESPONSES['how_much']"),
    _a(LEDGER, "fixed_trigger:why_me", ("BUILD_TRUST", "ADDRESS_OBJECTION"), "ambiguous_trust_or_objection_transparency",
       "main._FIXED_TRIGGER_RESPONSES['why_me']"),
    _u(LEDGER, "fixed_trigger:loan", "declines_loan_not_a_registry_action"),
    _u(LEDGER, "fixed_trigger:driving", "deferral_message_not_do_nothing"),
    _u(LEDGER, "fixed_trigger:working", "deferral_message_not_do_nothing"),
    # ---- events that are not ledger decisions: mapped for the record, never samples
    Rule(EVENT, "followup_sent", MappingStatus.MAPPED, "FOLLOW_UP", "followup_worker_fu", (), "deterministic",
         "main._followup_worker_tick sends FU1-FU3 after user silence", False),
    Rule(EVENT, "fixed_trigger:fraud", MappingStatus.MAPPED, "BUILD_TRUST", "fixed_fraud_license", (), "deterministic",
         "main.on_message fixed fraud branch sends the licence document", False),
    _a(EVENT, "fixed_trigger:how_much", ("BUILD_TRUST", "ADDRESS_OBJECTION"), "ambiguous_trust_or_objection_expectation",
       "main._FIXED_TRIGGER_RESPONSES['how_much']"),
    _a(EVENT, "fixed_trigger:why_me", ("BUILD_TRUST", "ADDRESS_OBJECTION"), "ambiguous_trust_or_objection_transparency",
       "main._FIXED_TRIGGER_RESPONSES['why_me']"),
    _u(EVENT, "fixed_trigger:loan", "declines_loan_not_a_registry_action"),
    _u(EVENT, "fixed_trigger:driving", "deferral_message_not_do_nothing"),
    _u(EVENT, "fixed_trigger:working", "deferral_message_not_do_nothing"),
    _u(EVENT, "insult_block", "defensive_reply_not_a_registry_action"),
    _u(EVENT, "manual_mode", "no_event_recorded_for_takeover"),
    _u(EVENT, "a3", "analyst_service_not_conversational"),
)
RULES = {MAPPING_VERSION: RULES_V2}


def validate_rules(rules=RULES_V2, registry: ActionRegistry = DEFAULT_REGISTRY) -> None:
    """Two different sample-capable behaviours may never share an action; every action id must be registered."""
    seen: dict[str, str] = {}
    for r in rules:
        for a in ((r.action_id,) if r.action_id else ()) + r.candidates:
            if a not in registry:
                raise ValueError(f"rule {r.key}: unregistered action {a}")
        if r.status is MappingStatus.MAPPED and r.sample_capable:
            if seen.setdefault(r.action_id, r.behaviour_id) != r.behaviour_id:
                raise ValueError(f"action {r.action_id} would merge two different behaviours")
    keys = [(r.unit, r.key) for r in rules]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate rule keys")


validate_rules()


def _norm(v):
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _lookup(unit: str, key: str) -> Rule | None:
    return next((r for r in RULES_V2 if r.unit == unit and r.key == key), None)


def _guard(res: MappingResult, registry: ActionRegistry) -> MappingResult:
    if res.action_id is not None and res.action_id not in registry:
        return MappingResult(MappingStatus.UNMAPPED, None, None, (), "action_not_in_registry:" + res.action_id, LEDGER, False)
    return res


def map_decision(regime, trigger, *, version: str = MAPPING_VERSION, registry: ActionRegistry = DEFAULT_REGISTRY,
                 decision_path="llm_reply") -> MappingResult:
    """Ledger decision -> MappingResult. `decision_path` names the branch ("llm_reply" | "followup" | "fixed_trigger"). Never raises;
    unknown inputs are UNMAPPED."""
    if version not in SUPPORTED_VERSIONS:
        return MappingResult(MappingStatus.UNMAPPED, None, None, (), "unsupported_mapping_version", LEDGER, False)
    path = _norm(decision_path) or "llm_reply"
    if version == MAPPING_VERSION and path != "llm_reply":
        if path == "followup":
            return _guard(_lookup(LEDGER, "path:followup").result(), registry)
        if path == "followup_holdout":
            return _guard(_lookup(LEDGER, "path:followup_holdout").result(), registry)
        if path == "fixed_trigger":
            t = _norm(trigger)
            if not t:
                return MappingResult(MappingStatus.UNMAPPED, None, None, (), "fixed_trigger_name_unknown", LEDGER, False)
            rule = _lookup(LEDGER, "fixed_trigger:" + t)
            return _guard(rule.result(), registry) if rule is not None else MappingResult(
                MappingStatus.UNMAPPED, None, None, (), "fixed_trigger_not_in_mapping:" + t, LEDGER, False)
        return MappingResult(MappingStatus.UNMAPPED, None, None, (), "decision_path_not_in_mapping:" + path, LEDGER, False)
    r, t = _norm(regime), _norm(trigger)
    if version == MAPPING_VERSION_V1:                                # legacy reproduction only
        if r == "hard" and "ADDRESS_OBJECTION" in registry:
            return MappingResult(MappingStatus.MAPPED, "ADDRESS_OBJECTION", "regime_hard_legacy", (), "legacy v1: regime hard", LEDGER, True)
        return MappingResult(MappingStatus.UNMAPPED, None, None, (), "regime_unmapped:" + (r or "unknown"), LEDGER, False)
    if t:
        rule = _lookup(LEDGER, "trigger:" + t)
        if rule is not None:
            res = rule.result()
        elif _lookup(EVENT, "fixed_trigger:" + t) is not None or t in ("fraud", "driving", "working", "how_much", "why_me", "loan"):
            res = MappingResult(MappingStatus.UNMAPPED, None, None, (), "fixed_trigger_is_not_an_llm_decision", LEDGER, False)
        else:
            res = MappingResult(MappingStatus.UNMAPPED, None, None, (), "trigger_not_in_mapping:" + t, LEDGER, False)
    elif r is None:
        res = MappingResult(MappingStatus.UNMAPPED, None, None, (), "regime_unknown", LEDGER, False)
    else:
        rule = _lookup(LEDGER, "regime:" + r)
        res = rule.result() if rule is not None else MappingResult(MappingStatus.UNMAPPED, None, None, (), "regime_not_in_mapping:" + r, LEDGER, False)
    if res.action_id is not None and res.action_id not in registry:
        return MappingResult(MappingStatus.UNMAPPED, None, None, (), "action_not_in_registry:" + res.action_id, LEDGER, False)
    return res


def map_event(event_type, name=None) -> MappingResult:
    """Event-level mapping (ai_activity event type [+ fixed trigger name]). Never a sample (sample_capable False)."""
    e, n = _norm(event_type), _norm(name)
    for key in ((e + ":" + n) if e and n else None, e):
        rule = _lookup(EVENT, key) if key else None
        if rule is not None:
            return rule.result()
    return MappingResult(MappingStatus.UNMAPPED, None, None, (), "event_not_in_mapping:" + (e or "unknown"), EVENT, False)


def rules_fingerprint(version: str = MAPPING_VERSION) -> str:
    """Content hash of the rule set, so a rule change is visible in the audit trail even when the version string is kept."""
    import hashlib
    import json
    rows = [[r.unit, r.key, r.status.value, r.action_id, r.behaviour_id, list(r.candidates), r.reason, r.sample_capable] for r in RULES_V2] \
        if version == MAPPING_VERSION else [version]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:16]


def summary(rules=RULES_V2) -> Mapping[str, int]:
    out = {s.value: 0 for s in MappingStatus}
    for r in rules:
        out[r.status.value] += 1
    return out

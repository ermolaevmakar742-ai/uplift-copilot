"""Evidence Arbiter v1: deterministic, rule-based, pure (no I/O). Shadow only.

arbitrate(field, items) -> canonical decision for ONE state field. Never picks a
winner by source type: each item gets weight = confidence*freshness*directness*
reliability; values are compared by total weight and a significant opposing
weight keeps the field UNRESOLVED (the conflict is reported, never hidden).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

ARBITER_VERSION = "arbiter-1"
FAMILIES = ("ai", "declared", "telegram", "client_state")
HALF_LIFE_H = 72.0          # freshness halves every 72h
CONFLICT_RATIO = 0.35       # opposing weight / total weight >= this -> unresolved
MIN_WEIGHT = 0.15           # total weight below this -> unresolved (weak evidence)
DEFAULT_RELIABILITY = {"ai": 0.6, "declared": 0.7, "telegram": 0.9, "client_state": 0.7}


@dataclass(frozen=True)
class EvidenceItem:
    ref: str                       # opaque reference (no raw payloads)
    source_family: str
    value: object
    confidence: float = 0.5
    observed_at: datetime | None = None
    directness: float = 0.5        # 1.0 = said by the client / first-hand, 0.0 = inferred
    reliability: float | None = None
    source_id: str | None = None   # independent source within a family
    is_guess: bool = False         # a guessed observation can never confirm a value on its own


@dataclass
class CanonicalDecision:
    field: str
    status: str                    # "resolved" | "unresolved" | "no_evidence"
    value: object = None
    confidence: float = 0.0
    contradiction: bool = False
    supporting: list = field(default_factory=list)
    opposing: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)   # [{"value", "weight"}] sorted
    reason: str = ""
    version: str = ARBITER_VERSION

    def to_dict(self) -> dict:
        return {"field": self.field, "status": self.status, "value": self.value,
                "confidence": self.confidence, "contradiction": self.contradiction,
                "supporting": self.supporting, "opposing": self.opposing,
                "alternatives": self.alternatives, "reason": self.reason, "version": self.version}


def _clamp(x, default=0.5) -> float:
    return max(0.0, min(1.0, float(x))) if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _aware(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def freshness(observed_at, now: datetime) -> float:
    ts = _aware(observed_at)
    if ts is None:
        return 0.5                                  # unknown age: neutral, never "fresh"
    age_h = max(0.0, (now - ts).total_seconds() / 3600)
    return 0.5 ** (age_h / HALF_LIFE_H)


def weight(it: EvidenceItem, now: datetime) -> float:
    rel = it.reliability if it.reliability is not None else DEFAULT_RELIABILITY.get(it.source_family, 0.5)
    return _clamp(it.confidence) * freshness(it.observed_at, now) * _clamp(it.directness) * _clamp(rel)


def _key(v):
    return v.strip().lower() if isinstance(v, str) else repr(v)


def arbitrate(field_name: str, items, now: datetime | None = None) -> CanonicalDecision:
    now = _aware(now) or datetime.now(timezone.utc)
    items = [i for i in (items or ()) if isinstance(i, EvidenceItem) and i.value is not None]
    if not items:
        return CanonicalDecision(field_name, "no_evidence", reason="no_evidence")
    groups: dict = {}
    for it in sorted(items, key=lambda i: i.ref):          # order-independent
        g = groups.setdefault(_key(it.value), {"value": it.value, "items": [], "w": 0.0, "indep": set()})
        g["items"].append(it)
        g["w"] += weight(it, now)
        g["indep"].add((it.source_family, it.source_id or it.ref))
    ranked = sorted(groups.values(), key=lambda g: (-g["w"], _key(g["value"])))
    total = sum(g["w"] for g in ranked)
    top = ranked[0]
    opp = ranked[1:]
    opp_w = total - top["w"]
    alts = [{"value": g["value"], "weight": round(g["w"], 4)} for g in ranked]
    support = [i.ref for i in top["items"]]
    opposing = [i.ref for g in opp for i in g["items"]]
    contradiction = bool(opp)
    if all(i.is_guess for i in top["items"]):
        return CanonicalDecision(field_name, "unresolved", None, round(min(top["w"], 0.5), 3), contradiction,
                                 support, opposing, alts, "guess_only")
    if total < MIN_WEIGHT:
        return CanonicalDecision(field_name, "unresolved", None, 0.0, contradiction, support, opposing,
                                 alts, "weak_evidence")
    if total > 0 and opp_w / total >= CONFLICT_RATIO:
        return CanonicalDecision(field_name, "unresolved", None, round(top["w"] / total * 0.5, 3), True,
                                 support, opposing, alts, "significant_contradiction")
    # agreement bonus: independent sources raise confidence (capped), a minority dissent lowers it
    base = max(weight(i, now) for i in top["items"])
    bonus = 0.1 * (len(top["indep"]) - 1)
    conf = _clamp(base + bonus) * (1 - (opp_w / total if total else 0))
    return CanonicalDecision(field_name, "resolved", top["value"], round(conf, 3), contradiction, support,
                             opposing, alts, "weak_contradiction" if contradiction else
                             ("independent_agreement" if len(top["indep"]) > 1 else "single_source"))


def arbitrate_state(evidence_by_field: dict, now: datetime | None = None) -> dict:
    return {f: arbitrate(f, its, now).to_dict() for f, its in sorted((evidence_by_field or {}).items())}


# ---- adapter: existing Client State -> evidence items (read-only, no raw text) ----
def evidence_from_client_state(user, client_state, analysis: dict | None = None,
                               now: datetime | None = None) -> dict:
    cs = client_state if isinstance(client_state, dict) else {}
    conv, ident = cs.get("conversation") or {}, cs.get("identity") or {}
    ev: dict = {}

    def add(fname, it):
        ev.setdefault(fname, []).append(it)

    stage_user = getattr(user, "stage", None)
    if stage_user:
        add("stage", EvidenceItem("telegram:user.stage", "telegram", stage_user, 0.8, directness=0.8))
    if cs.get("stage"):
        add("stage", EvidenceItem("client_state:stage", "client_state", cs["stage"], 0.7, directness=0.6))
    if conv.get("intent") not in (None, "unknown"):
        add("intent", EvidenceItem("ai:conversation.intent", "ai", conv["intent"], 0.6, directness=0.5))
    region = ident.get("region")
    if isinstance(region, str) and region:
        add("region", EvidenceItem("telegram:identity.region", "telegram", region, 0.7, directness=0.7))
    return ev


# ---- adapter: structured Evidence objects -> arbiter items, grouped by field ----
def url_token(url: str) -> str:
    """Stable pseudonym of a reference URL: the shadow record never carries the profile URL / handle."""
    import hashlib
    return "url_" + hashlib.sha1(url.encode()).hexdigest()[:12]


def items_from_evidence(evidence_list, now: datetime | None = None) -> dict:
    """One field per platform: 'profile.<platform>' whose value is the reference URL. Reads only the structured
    metadata set by the adapter (source_family, platform, is_guess, directness, reliability)."""
    out: dict = {}
    for ev in evidence_list or ():
        md = dict(getattr(ev, "metadata", None) or {})
        platform, url = md.get("platform"), (getattr(ev, "value", None) or {}).get("reference_url")
        if not platform or not url:
            continue
        out.setdefault("profile." + platform, []).append(EvidenceItem(
            ref=ev.evidence_id, source_family=md.get("source_family", "declared"), value=url_token(url),
            confidence=ev.confidence if ev.confidence is not None else 0.0, observed_at=ev.observed_at,
            directness=md.get("directness", 0.5), reliability=ev.reliability,
            source_id=md.get("source_family", "declared"), is_guess=bool(md.get("is_guess"))))
    return out

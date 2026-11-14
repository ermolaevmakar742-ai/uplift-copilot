"""Decision Evidence Snapshot (decision_evidence.v1): which Evidence was available for ONE production
decision. A pure, immutable, historical record. It does not decide, score, rank, pick a winner or
compute any final stage/intent/sentiment/trust/readiness. Pure contract: no DB, no persistence, no
network, no LLM; stdlib + copilot_evidence only. It does NOT import copilot_decisions.

decision_id is a reference to the Production Decision Ledger id ("pd_..." format, checked by shape
only, never looked up). The ProductionDecision itself is not copied. message_id may be None. created_at
is the snapshot's own creation time; contained Evidence keep their original timestamps.

Contents: Evidence (sorted with Fusion's deterministic key), the EvidenceConflict records and the
DerivedLink records of the FusionResult (links are kept so derived evidence is never mistaken for an
independent observation). Evidence is deep-immutable, so the snapshot cannot change after creation and
holds no live reference to Client State or to the source FusionResult.

Integrity checks at construction: every Evidence belongs to chat_id; PRODUCTION evidence (the
decision's own ACTION) must carry source_id == decision_id; conflicts/links only reference contained
evidence; provenance equals the one derived from the evidence (nothing fabricated); the
decision_evidence_id equals the deterministic content hash (created_at excluded).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from copilot_evidence.contracts import (ContractError, Evidence, EvidenceSource, _dt, _freeze, _int, _thaw)
from copilot_evidence.fusion import (DerivedLink, EvidenceConflict, FusionResult,
                                  evidence_sort_key)

DECISION_EVIDENCE_CONTRACT_VERSION = "decision_evidence.v1"
DECISION_ID_PREFIX = "pd_"      # shape of Production Decision Ledger ids (no import of copilot_decisions)
ID_PREFIX = "de_"


@dataclass(frozen=True)
class EvidenceRef:
    source: str
    source_id: str | None
    evidence_id: str
    contract_version: str

    def to_dict(self) -> dict:
        return {"source": self.source, "source_id": self.source_id, "evidence_id": self.evidence_id,
                "contract_version": self.contract_version}

    @classmethod
    def from_dict(cls, d: Mapping) -> "EvidenceRef":
        _keys(d, ("source", "source_id", "evidence_id", "contract_version"), "evidence_ref")
        return cls(d["source"], d["source_id"], d["evidence_id"], d["contract_version"])


@dataclass(frozen=True)
class SnapshotProvenance:
    decision_id: str
    fusion_version: str
    evidence_refs: tuple[EvidenceRef, ...]

    def to_dict(self) -> dict:
        return {"decision_id": self.decision_id, "fusion_version": self.fusion_version,
                "evidence_refs": [r.to_dict() for r in self.evidence_refs]}

    @classmethod
    def from_dict(cls, d: Mapping) -> "SnapshotProvenance":
        _keys(d, ("decision_id", "fusion_version", "evidence_refs"), "provenance")
        if not isinstance(d["evidence_refs"], (list, tuple)):
            raise ContractError("evidence_refs must be a list")
        return cls(d["decision_id"], d["fusion_version"], tuple(EvidenceRef.from_dict(r) for r in d["evidence_refs"]))


def _keys(d: Any, keys: tuple, what: str) -> None:
    if not isinstance(d, Mapping):
        raise ContractError(f"{what} must be a mapping")
    missing = [k for k in keys if k not in d]
    extra = sorted(set(d) - set(keys))
    if missing or extra:
        raise ContractError(f"{what}: missing={missing} unknown={extra}")


def _str_tuple(name: str, v: Any) -> tuple:
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        raise ContractError(f"{name} must be a sequence of str")
    return tuple(v)


def _refs(evidence: tuple[Evidence, ...]) -> tuple[EvidenceRef, ...]:
    return tuple(EvidenceRef(e.source.value, e.provenance.source_id, e.evidence_id, e.contract_version)
                 for e in evidence)


def _compute_id(decision_id: str, chat_id: int, message_id: int | None, evidence: tuple[Evidence, ...]) -> str:
    obs = []
    for e in evidence:
        d = e.to_dict()
        d.pop("created_at")
        obs.append(d)
    raw = json.dumps([DECISION_EVIDENCE_CONTRACT_VERSION, decision_id, chat_id, message_id, obs],
                     sort_keys=True, separators=(",", ":"))
    return ID_PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class DecisionEvidence:
    decision_evidence_id: str
    decision_id: str
    chat_id: int
    message_id: int | None
    created_at: datetime
    evidence: tuple[Evidence, ...]
    conflicts: tuple[EvidenceConflict, ...]
    provenance: SnapshotProvenance
    links: tuple[DerivedLink, ...] = ()
    contract_version: str = DECISION_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        did = self.decision_id
        if not (isinstance(did, str) and did.startswith(DECISION_ID_PREFIX) and len(did) > len(DECISION_ID_PREFIX)):
            raise ContractError("decision_id must be a Production Decision Ledger id ('pd_...')")
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "message_id", _int("message_id", self.message_id, optional=True))
        s(self, "created_at", _dt("created_at", self.created_at))
        if self.contract_version != DECISION_EVIDENCE_CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")
        if not isinstance(self.evidence, (list, tuple)) or not all(isinstance(e, Evidence) for e in self.evidence):
            raise ContractError("evidence must be a sequence of Evidence")
        ev = tuple(sorted(self.evidence, key=evidence_sort_key))
        ids = [e.evidence_id for e in ev]
        for e in ev:
            if e.chat_id != self.chat_id:
                raise ContractError("evidence belongs to a different chat")
            if e.source is EvidenceSource.PRODUCTION and e.provenance.source_id != did:
                raise ContractError("production evidence belongs to a different decision")
        s(self, "evidence", ev)

        conflicts = []
        if not isinstance(self.conflicts, (list, tuple)) or not all(isinstance(c, EvidenceConflict) for c in self.conflicts):
            raise ContractError("conflicts must be a sequence of EvidenceConflict")
        for c in self.conflicts:
            if not set(c.evidence_ids) <= set(ids):
                raise ContractError("conflict references evidence outside the snapshot")
            if not (len(c.evidence_ids) == len(c.values) == len(c.sources)) or len(c.evidence_ids) < 2:
                raise ContractError("malformed conflict")
            conflicts.append(EvidenceConflict(c.conflict_id, tuple(c.evidence_ids), c.field,
                                              tuple(_freeze(v) for v in c.values), tuple(c.sources)))
        s(self, "conflicts", tuple(sorted(conflicts, key=lambda c: (c.field, c.conflict_id))))

        if not isinstance(self.links, (list, tuple)) or not all(isinstance(l, DerivedLink) for l in self.links):
            raise ContractError("links must be a sequence of DerivedLink")
        for l in self.links:
            if l.derived_id not in ids or (l.origin_id is not None and l.origin_id not in ids):
                raise ContractError("link references evidence outside the snapshot")
        s(self, "links", tuple(sorted(self.links, key=lambda l: (l.derived_id, l.derived_from))))

        p = self.provenance
        if not isinstance(p, SnapshotProvenance) or p.decision_id != did or tuple(p.evidence_refs) != _refs(ev):
            raise ContractError("provenance does not match decision_id / evidence")
        s(self, "provenance", SnapshotProvenance(did, p.fusion_version, _refs(ev)))
        if self.decision_evidence_id != _compute_id(did, self.chat_id, self.message_id, ev):
            raise ContractError("decision_evidence_id does not match content")

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "decision_evidence_id": self.decision_evidence_id,
            "decision_id": self.decision_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "created_at": self.created_at.isoformat(),
            "evidence": [e.to_dict() for e in self.evidence],
            "conflicts": [{"conflict_id": c.conflict_id, "evidence_ids": list(c.evidence_ids),
                           "field": c.field, "values": _thaw(c.values), "sources": list(c.sources)}
                          for c in self.conflicts],
            "links": [{"derived_id": l.derived_id, "origin_id": l.origin_id, "derived_from": l.derived_from,
                       "resolution": l.resolution, "independent": l.independent} for l in self.links],
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "DecisionEvidence":
        _keys(d, ("contract_version", "decision_evidence_id", "decision_id", "chat_id", "message_id",
                  "created_at", "evidence", "conflicts", "links", "provenance"), "decision_evidence")
        for k in ("evidence", "conflicts", "links"):
            if not isinstance(d[k], (list, tuple)):
                raise ContractError(f"{k} must be a list")
        conflicts = []
        for c in d["conflicts"]:
            _keys(c, ("conflict_id", "evidence_ids", "field", "values", "sources"), "conflict")
            if not isinstance(c["conflict_id"], str) or not isinstance(c["field"], str):
                raise ContractError("malformed conflict")
            if not isinstance(c["values"], (list, tuple)):
                raise ContractError("conflict values must be a list")
            conflicts.append(EvidenceConflict(c["conflict_id"], _str_tuple("evidence_ids", c["evidence_ids"]),
                                              c["field"], tuple(c["values"]), _str_tuple("sources", c["sources"])))
        links = []
        for l in d["links"]:
            _keys(l, ("derived_id", "origin_id", "derived_from", "resolution", "independent"), "link")
            if not isinstance(l["independent"], bool):
                raise ContractError("link.independent must be bool")
            links.append(DerivedLink(l["derived_id"], l["origin_id"], l["derived_from"], l["resolution"],
                                     l["independent"]))
        return cls(decision_evidence_id=d["decision_evidence_id"], decision_id=d["decision_id"],
                   chat_id=d["chat_id"], message_id=d["message_id"], created_at=d["created_at"],
                   evidence=tuple(Evidence.from_dict(e) for e in d["evidence"]), conflicts=tuple(conflicts),
                   provenance=SnapshotProvenance.from_dict(d["provenance"]), links=tuple(links),
                   contract_version=d["contract_version"])


def build_decision_evidence(decision_id: str, fusion_result: FusionResult, *, chat_id: int,
                            message_id: int | None, created_at: datetime) -> DecisionEvidence:
    """Snapshot a FusionResult for one production decision. Raises ContractError on invalid input."""
    if not isinstance(fusion_result, FusionResult):
        raise ContractError("fusion_result must be a FusionResult")
    ev = tuple(sorted(fusion_result.evidence, key=evidence_sort_key))
    try:
        eid = _compute_id(decision_id, chat_id, message_id, ev)
    except (TypeError, ValueError):
        raise ContractError("invalid decision_id / chat_id / message_id") from None
    return DecisionEvidence(
        decision_evidence_id=eid,
        decision_id=decision_id, chat_id=chat_id, message_id=message_id, created_at=created_at,
        evidence=ev, conflicts=tuple(fusion_result.conflicts),
        provenance=SnapshotProvenance(decision_id, fusion_result.fusion_version, _refs(ev)),
        links=tuple(fusion_result.links))

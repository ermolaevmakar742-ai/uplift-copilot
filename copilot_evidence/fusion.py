"""Deterministic Evidence Fusion v1 (fusion.v1): analytical, read-only, LOSSLESS.

fuse(items) -> FusionResult. Does only: validation, exact-duplicate removal, derived-evidence
links, explicit conflict records. It never scores, weights, ranks sources, aggregates confidence,
picks a winner, computes a final stage/intent, or recommends anything. Absence of evidence is never
turned into negative evidence. No I/O, no network, no LLM, no DB; stdlib + copilot_evidence.contracts.

Validation: only Evidence instances are accepted; anything else is listed in `rejected` (sorted by
reason/type, not by position, so the result is independent of input order) and never breaks the batch.

Dedup: two items are the SAME observation when they are identical in every field except created_at
(evidence_id included). The earliest created_at is kept, `duplicates` records how many copies were
dropped. Equal evidence_type alone never dedups.

Derived: metadata.derived_from marks an observation derived from another one. Known evidence-origin
("deposit_path.blocker" -> the BLOCKER Evidence with the same chat/message/provenance) is linked
(resolution="resolved"); other values are kept with origin_id=None ("origin_not_evidence" /
"origin_missing" / "ambiguous"). Derived evidence is kept, never independent (independent=False).

Conflicts (explicit EvidenceConflict, all evidence kept) only between CLIENT observations of the same chat:
  STAGE      same message_id (None==None), canonical stage differs; UNKNOWN stages are not claims
  INTENT     same message_id, value differs
  SENTIMENT  same message_id, value differs (exact inequality; no tolerance is invented)
  CLIENT_FACT same fact field (client "region" is treated as OSINT "geo"), case-insensitive value differs
  evidence_id the same evidence_id carrying different observations
ACTION (system action) and all other types never conflict. `values`/`sources` are aligned with `evidence_ids`.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from copilot_evidence.contracts import Evidence, EvidenceType

FUSION_VERSION = "fusion.v1"

DERIVED_ORIGIN = {"deposit_path.blocker": EvidenceType.BLOCKER}
FACT_FIELD_ALIASES = {"region": "geo"}


@dataclass(frozen=True)
class Rejected:
    reason: str
    type_name: str


@dataclass(frozen=True)
class Duplicate:
    evidence_id: str
    dropped: int


@dataclass(frozen=True)
class DerivedLink:
    derived_id: str
    origin_id: str | None
    derived_from: str
    resolution: str           # resolved | origin_not_evidence | origin_missing | ambiguous
    independent: bool = False


@dataclass(frozen=True)
class EvidenceConflict:
    conflict_id: str
    evidence_ids: tuple[str, ...]
    field: str
    values: tuple[Any, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class FusionResult:
    evidence: tuple[Evidence, ...] = ()
    links: tuple[DerivedLink, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    duplicates: tuple[Duplicate, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    fusion_version: str = FUSION_VERSION

    def provenance(self) -> tuple[tuple[str, str | None, str, str], ...]:
        """(source, source_id, evidence_id, contract_version) of every kept Evidence."""
        return tuple((e.source.value, e.provenance.source_id, e.evidence_id, e.contract_version)
                     for e in self.evidence)


def _json(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _obs_key(e: Evidence) -> str:
    d = e.to_dict()
    d.pop("created_at")
    return _json(d)


def _order_key(e: Evidence) -> tuple:
    return (e.source.value, e.evidence_type.value, e.evidence_id, _obs_key(e))


evidence_sort_key = _order_key   # public: the deterministic ordering used by Fusion


def _cid(*parts) -> str:
    return "cf_" + hashlib.sha256(_json(parts).encode()).hexdigest()[:16]


def _dedup(valid: list[Evidence]):
    groups: dict[str, list[Evidence]] = {}
    for e in valid:
        groups.setdefault(_obs_key(e), []).append(e)
    kept, dups = [], []
    for key in sorted(groups):
        g = sorted(groups[key], key=lambda e: e.created_at)
        kept.append(g[0])
        if len(g) > 1:
            dups.append(Duplicate(g[0].evidence_id, len(g) - 1))
    return sorted(kept, key=_order_key), tuple(sorted(dups, key=lambda d: (d.evidence_id, d.dropped)))


def _links(items: list[Evidence]) -> tuple[DerivedLink, ...]:
    out = []
    for e in items:
        d = e.metadata.get("derived_from")
        if not isinstance(d, str) or not d:
            continue
        origin_type = DERIVED_ORIGIN.get(d)
        if origin_type is None:
            out.append(DerivedLink(e.evidence_id, None, d, "origin_not_evidence"))
            continue
        cands = [o for o in items if o is not e and o.evidence_type is origin_type
                 and o.chat_id == e.chat_id and o.message_id == e.message_id
                 and o.provenance.source is e.provenance.source
                 and o.provenance.source_id == e.provenance.source_id]
        if len(cands) == 1:
            out.append(DerivedLink(e.evidence_id, cands[0].evidence_id, d, "resolved"))
        else:
            out.append(DerivedLink(e.evidence_id, None, d, "origin_missing" if not cands else "ambiguous"))
    return tuple(sorted(out, key=lambda l: (l.derived_id, l.derived_from)))


def _claim(e: Evidence):
    """(group key, comparable, display value) for a client observation, else None."""
    t = e.evidence_type
    if t is EvidenceType.STAGE:
        if e.stage.value == "UNKNOWN":
            return None
        return ("stage", e.message_id), e.stage.value, e.stage.value
    if t is EvidenceType.INTENT:
        if isinstance(e.value, str) and e.value.strip():
            return ("intent", e.message_id), e.value, e.value
    elif t is EvidenceType.SENTIMENT:
        if isinstance(e.value, (int, float)) and not isinstance(e.value, bool):
            return ("sentiment", e.message_id), e.value, e.value
    elif t is EvidenceType.CLIENT_FACT:
        v = e.value
        if hasattr(v, "get"):
            f = v.get("field") or v.get("category")
            x = v.get("value")
            if isinstance(f, str) and f and isinstance(x, str) and x.strip():
                return ("fact:" + FACT_FIELD_ALIASES.get(f, f), None), x.strip().lower(), x
    return None


def _conflicts(items: list[Evidence]) -> tuple[EvidenceConflict, ...]:
    out = []
    groups: dict[tuple, list[tuple[Evidence, Any, Any]]] = {}
    for e in items:
        c = _claim(e)
        if c:
            groups.setdefault((e.chat_id,) + c[0], []).append((e, c[1], c[2]))
    for key, g in groups.items():
        if len({cmp for _, cmp, _ in g}) < 2:
            continue
        g.sort(key=lambda t: _order_key(t[0]))
        ids = tuple(e.evidence_id for e, _, _ in g)
        field = key[1]
        out.append(EvidenceConflict(_cid(field, key[0], key[2], ids), ids, field,
                                    tuple(d for _, _, d in g), tuple(e.source.value for e, _, _ in g)))
    by_id: dict[str, list[Evidence]] = {}
    for e in items:
        by_id.setdefault(e.evidence_id, []).append(e)
    for eid, g in by_id.items():
        if len(g) > 1:
            g = sorted(g, key=_order_key)
            out.append(EvidenceConflict(_cid("evidence_id", eid), tuple(eid for _ in g), "evidence_id",
                                        tuple(_obs_key(e) for e in g), tuple(e.source.value for e in g)))
    return tuple(sorted(out, key=lambda c: (c.field, c.conflict_id)))


def fuse(evidence_items: Iterable) -> FusionResult:
    try:
        if isinstance(evidence_items, (str, bytes)) or not hasattr(evidence_items, "__iter__"):
            return FusionResult(rejected=(Rejected("not_an_iterable", type(evidence_items).__name__),))
        valid, rejected = [], []
        for x in evidence_items:
            if isinstance(x, Evidence):
                valid.append(x)
            else:
                rejected.append(Rejected("not_an_evidence", type(x).__name__))
        kept, dups = _dedup(valid)
        return FusionResult(tuple(kept), _links(kept), _conflicts(kept), dups,
                            tuple(sorted(rejected, key=lambda r: (r.reason, r.type_name))))
    except Exception as e:  # fail-safe: analytics never raise
        return FusionResult(rejected=(Rejected(f"fusion_error:{type(e).__name__}", "batch"),))

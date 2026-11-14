"""Versioned, immutable registry of knowledge cards + a pinned manifest. An assessment records `knowledge_version` and the registry
fingerprint; a card cannot change without a version bump because the manifest pins every id@version to its content hash."""
import json
from pathlib import Path
from types import MappingProxyType

from copilot_psychology.contracts import KNOWLEDGE_VERSION, ContractError, Domain, EvidenceLevel, KnowledgeCard, Role, Strategy, stable_hash
from copilot_psychology.knowledge import CARDS

MANIFEST_PATH = Path(__file__).with_name("manifest.json")
RESISTANCE_TAGS = frozenset({"none_detected", "trust_concern", "price_uncertainty", "payment_method_uncertainty", "technical_issue", "deferral",
                             "unspecified", "conflicting", "unknown"})


class KnowledgeRegistry:
    def __init__(self, cards, version: str = KNOWLEDGE_VERSION):
        self.version = version
        by_key = {}
        for c in cards:
            c.validate()
            if c.key in by_key:
                raise ContractError(f"duplicate card {c.key}")
            for tag in c.applicability:
                if tag.startswith("resistance:") and tag.split(":", 1)[1] not in RESISTANCE_TAGS:
                    raise ContractError(f"{c.key}: unknown applicability tag {tag}")
            by_key[c.key] = c
        missing = set(Domain) - {c.domain for c in by_key.values()}
        if missing:
            raise ContractError(f"domains without a card: {sorted(m.value for m in missing)}")
        self._cards = MappingProxyType(by_key)

    def get(self, key: str) -> KnowledgeCard | None:
        return self._cards.get(key)

    def all(self) -> tuple[KnowledgeCard, ...]:
        return tuple(self._cards[k] for k in sorted(self._cards))

    def by_domain(self, domain: Domain) -> tuple[KnowledgeCard, ...]:
        return tuple(c for c in self.all() if c.domain is domain)

    def for_resistance(self, resistance: str, strategy: Strategy) -> tuple[KnowledgeCard, ...]:
        return tuple(c for c in self.all() if c.role is Role.STRATEGY_INPUT and strategy in c.candidate_strategies
                     and f"resistance:{resistance}" in c.applicability)

    def manifest(self) -> dict:
        return {k: self._cards[k].card_hash() for k in sorted(self._cards)}

    def fingerprint(self) -> str:
        return stable_hash([self.version, self.manifest()], "kf_", 16)

    def weakest_level(self, cards) -> EvidenceLevel | None:
        levels = [c.evidence_level for c in cards]
        return max(levels, key=lambda l: l.rank) if levels else None


REGISTRY = KnowledgeRegistry(CARDS)


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_manifest(registry: KnowledgeRegistry = REGISTRY) -> list[str]:
    """Problems between the pinned manifest and the registry: a changed card without a version bump, or a removed card."""
    pinned, now, problems = load_manifest()["cards"], registry.manifest(), []
    for key, h in pinned.items():
        if key not in now:
            problems.append(f"removed: {key}")
        elif now[key] != h:
            problems.append(f"changed without a new version: {key}")
    return problems


def write_manifest() -> None:                      # a deliberate, reviewed act (python3 -m copilot_psychology.registry)
    MANIFEST_PATH.write_text(json.dumps({"knowledge_version": KNOWLEDGE_VERSION, "cards": REGISTRY.manifest()}, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_manifest()
    print("manifest written:", len(REGISTRY.manifest()), "cards;", REGISTRY.fingerprint())

"""Action registry: stable ids, never free text. Extensible by creating a NEW registry (immutable).

DO_NOTHING is the baseline action, but a missing message is NOT a control group: control needs randomization information
(see estimator). The production router is not changed: this registry only NAMES actions; the mapping from what production
really did to a registry action is explicit and partial:
  regime "hard"  -> ADDRESS_OBJECTION   (Shadow already treats production's `hard` regime as HANDLE_OBJECTION)
  regime "agent" / "casual" / "retention" -> unmapped (no registry action describes "present offer" / "continue dialogue")
Unmapped is UNKNOWN, never a guess."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

REGISTRY_VERSION = "actions.v1"
BASELINE_ACTION_ID = "DO_NOTHING"


class UnknownAction(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    action_id: str
    description: str
    version: str = "1"
    baseline: bool = False

    def __post_init__(self):
        if not (isinstance(self.action_id, str) and self.action_id.isupper() and self.action_id.replace("_", "").isalnum()):
            raise ValueError("action_id must be a stable UPPER_SNAKE identifier")
        if not self.description.strip() or not self.version.strip():
            raise ValueError("description and version are required")


class ActionRegistry:
    def __init__(self, actions=(), version: str = REGISTRY_VERSION):
        items: dict[str, Action] = {}
        for a in actions:
            if not isinstance(a, Action):
                raise ValueError("registry holds Action objects")
            if a.action_id in items:
                raise ValueError(f"duplicate action_id {a.action_id}")
            items[a.action_id] = a
        if sum(1 for a in items.values() if a.baseline) > 1:
            raise ValueError("at most one baseline action")
        self._items: Mapping[str, Action] = MappingProxyType(items)
        self.version = version

    def __contains__(self, action_id) -> bool:
        return isinstance(action_id, str) and action_id in self._items

    def get(self, action_id: str) -> Action:
        if action_id not in self:
            raise UnknownAction(f"unknown action_id {action_id!r}")
        return self._items[action_id]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def non_baseline_ids(self) -> tuple[str, ...]:
        return tuple(i for i in self.ids() if not self._items[i].baseline)

    def extended(self, *actions: Action, version: str) -> "ActionRegistry":
        return ActionRegistry([*self._items.values(), *actions], version)


DEFAULT_REGISTRY = ActionRegistry([
    Action("DO_NOTHING", "No active conversational move (baseline; NOT a control group by itself)", baseline=True),
    Action("ASK_QUESTION", "Ask the client a clarifying question"),
    Action("BUILD_TRUST", "Address trust concerns"),
    Action("ADDRESS_OBJECTION", "Address a stated objection"),
    Action("CLARIFY_PAYMENT", "Clarify payment method / amount"),
    Action("REGISTRATION_GUIDANCE", "Guide the client through registration"),
    Action("DEPOSIT_GUIDANCE", "Guide the client through a deposit"),
    Action("FOLLOW_UP", "Follow up after silence"),
    Action("HUMAN_HANDOFF", "Hand the conversation to a human"),
])

REGIME_TO_ACTION: Mapping[str, str] = MappingProxyType({"hard": "ADDRESS_OBJECTION"})
UNMAPPED_REGIMES = MappingProxyType({
    "agent": "present-offer has no registry action", "casual": "continue-dialogue has no registry action",
    "retention": "continue-dialogue has no registry action"})


def action_for_regime(regime, registry: ActionRegistry = DEFAULT_REGISTRY) -> tuple[str | None, str]:
    """(action_id | None, reason). Only an explicit, registered mapping yields an action."""
    r = regime.strip().lower() if isinstance(regime, str) else None
    if not r:
        return None, "regime_unknown"
    a = REGIME_TO_ACTION.get(r)
    if a is not None and a in registry:
        return a, "mapped"
    return None, "regime_unmapped:" + r if r in UNMAPPED_REGIMES else "regime_not_in_mapping:" + r

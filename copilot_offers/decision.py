"""Pure mapping: (OfferInput, OfferResolution) -> OfferDecisionResult. No I/O, clock, network or LLM. No selection."""
from copilot_contracts.stages import map_production_stage

from .models import MODE_SHADOW, POLICY_VERSION, OfferDecisionResult, OfferInput, OfferResolution, new_offer_decision_id


def decide(inp: OfferInput, resolution: OfferResolution, *, offer_decision_id: str | None = None) -> OfferDecisionResult:
    # Only identifiers and enums are copied: the resolution's context (URL, promo, images, text) never reaches the record.
    return OfferDecisionResult(
        offer_decision_id or new_offer_decision_id(), inp.correlation_id, inp.client_ref, inp.account_ref, resolution.program_id,
        resolution.status, resolution.reason, POLICY_VERSION, map_production_stage(inp.stage_raw).value,
        inp.stage_raw if isinstance(inp.stage_raw, str) else None, MODE_SHADOW)

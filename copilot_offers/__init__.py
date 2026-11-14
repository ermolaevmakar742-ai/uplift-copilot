"""COPILOT Account Offer Manager (V1, SHADOW ONLY): 1 Telegram account -> 1 assigned offer. The runtime hook only records which offer
the account has (offer_decisions); it never touches replies, prompts, history, client state, funnel, LLM, postback or the network."""
from .hook import schedule_offer_shadow
from .models import POLICY_VERSION, OfferDecisionResult, OfferReason, OfferStatus

__all__ = ["schedule_offer_shadow", "POLICY_VERSION", "OfferDecisionResult", "OfferReason", "OfferStatus"]

"""Minimal explicit action contract. No side effects: these are labels only.

Every action maps to something production COPILOT really has today
(select_ai_route / detect_objection_regime / manual_mode). Deliberately
absent: ASK_QUESTION (no dedicated production mode) and FOLLOW_UP (time-driven
by followup_worker, not a per-message decision).
"""
from enum import Enum


class ShadowAction(str, Enum):
    WAIT = "WAIT"                          # baseline: no active reply move
    CONTINUE_DIALOGUE = "CONTINUE_DIALOGUE"  # prod regimes: casual, retention
    HANDLE_OBJECTION = "HANDLE_OBJECTION"    # prod regime: hard
    PRESENT_OFFER = "PRESENT_OFFER"          # prod regime: agent
    HUMAN_HANDOFF = "HUMAN_HANDOFF"          # prod concept: manual_mode


# Production regime -> comparable action label (a PROXY of what production did).
REGIME_TO_ACTION = {
    "casual": ShadowAction.CONTINUE_DIALOGUE,
    "retention": ShadowAction.CONTINUE_DIALOGUE,
    "hard": ShadowAction.HANDLE_OBJECTION,
    "agent": ShadowAction.PRESENT_OFFER,
}

ALL_ACTIONS = tuple(ShadowAction)

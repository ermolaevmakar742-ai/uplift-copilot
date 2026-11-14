"""Versions, vocabulary, lifecycle and the PII scrubber of the learning layer. Pure."""
import hashlib
import json
import os
import re

CRITIC_VERSION = "dialogue-critic-1"
PATTERN_VERSION = "pattern-1"
LEARNER_VERSION = "learner-1"
EXAMPLE_VERSION = "example-1"
IMPROVEMENT_VERSION = "improvement-1"
OFFLINE_EVAL_VERSION = "offline-eval-1"
FLAG = "COPILOT_DIALOGUE_LEARNING_SHADOW_ENABLED"

# minimal, versioned reason vocabulary (extend by bumping CRITIC_VERSION)
NEGATIVE_REASONS = ("TOO_EARLY_OFFER", "IGNORED_OBJECTION", "NO_QUESTION", "REPETITIVE", "WRONG_STAGE", "TOO_LONG", "TOO_PUSHY")
POSITIVE_REASONS = ("GOOD_STATE_ALIGNMENT", "GOOD_OBJECTION_HANDLING", "GOOD_NEXT_QUESTION")
META_REASONS = ("LOW_CONFIDENCE",)
ALL_REASONS = NEGATIVE_REASONS + POSITIVE_REASONS + META_REASONS
DIMENSIONS = ("relevance", "state_alignment", "stage_fit", "objection_fit", "question_quality", "clarity", "trust",
              "pressure", "response_risk", "overall_quality")      # all 0..100, HIGHER IS BETTER (100 = no pressure / no risk)

# improvement lifecycle: only the first three states are reachable in v1
LIFECYCLE = ("OBSERVED", "CANDIDATE", "OFFLINE_EVALUATED", "HUMAN_REVIEW", "APPROVED", "REJECTED")
REACHABLE_V1 = ("OBSERVED", "CANDIDATE", "OFFLINE_EVALUATED")


def learning_enabled() -> bool:
    return os.getenv(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


_PII = [(re.compile(r"https?://\S+|www\.\S+", re.I), "<url>"), (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
        (re.compile(r"(?<!\w)@\w{3,}"), "<handle>"), (re.compile(r"\+?\d[\d\s().-]{6,}\d"), "<number>"),
        (re.compile(r"\b\d{5,}\b"), "<number>"), (re.compile(r"\bt\.me/\S+", re.I), "<url>")]


def scrub(text, limit: int = 240) -> str:
    """Remove URLs, emails, @handles and long digit runs; truncate. Applied to ANY free text before it can be stored."""
    s = text if isinstance(text, str) else ""
    for rx, rep in _PII:
        s = rx.sub(rep, s)
    return " ".join(s.split())[:limit]


def digest(*parts, n: int = 16) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:n]


def response_ref(text) -> dict:
    """Reference to the original response WITHOUT storing it: content hash + length."""
    t = text if isinstance(text, str) else ""
    return {"sha1": hashlib.sha1(t.encode()).hexdigest()[:20], "chars": len(t)}

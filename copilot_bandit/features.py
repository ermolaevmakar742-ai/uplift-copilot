"""Bandit context from the EXISTING features.v1 (copilot_uplift.features); no second feature extraction.

features_ref is a content hash of (feature_version, feature id/value pairs): stable, reproducible from the PII-free
shadow snapshot, free of ids and timestamps. The vector itself is never stored."""
import hashlib
import json
import math
from datetime import datetime, timezone

from copilot_uplift.features import FEATURE_VERSION, build_features

STAGES = ("START", "INTEREST", "NEED", "TRUST", "OBJECTION", "PAYMENT", "DEPOSIT", "SUCCESS", "DROPPED")
INTENTS = ("greeting", "question", "objection", "ready", "statement")
DIM = len(STAGES) + len(INTENTS) + 4


def feature_vector(decision_id: str, chat_id: int, snapshot, as_of: datetime | None = None):
    """features.v1 FeatureVector from a shadow/client-state snapshot dict (flat or nested)."""
    return build_features(decision_id, chat_id, as_of or datetime.now(timezone.utc), snapshot)


def features_ref(fv) -> str:
    pairs = [[f.feature_id, f.value] for f in fv.features]
    raw = json.dumps([fv.feature_version, pairs], sort_keys=True, separators=(",", ":"), default=str)
    return "fx_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def encode(fv) -> list:
    """Fixed-length numeric context (DIM floats): stage one-hot, intent one-hot, sentiment, messages, region_known, bias."""
    x = [0.0] * DIM
    stage, intent = fv.get("stage"), fv.get("intent")
    if stage in STAGES:
        x[STAGES.index(stage)] = 1.0
    if intent in INTENTS:
        x[len(STAGES) + INTENTS.index(intent)] = 1.0
    o = len(STAGES) + len(INTENTS)
    s = fv.get("sentiment")
    x[o] = min(1.0, max(0.0, float(s))) if isinstance(s, (int, float)) and not isinstance(s, bool) else 0.0
    m = fv.get("messages")
    x[o + 1] = min(1.0, math.log1p(m) / math.log1p(50)) if isinstance(m, int) and not isinstance(m, bool) and m >= 0 else 0.0
    x[o + 2] = 1.0 if fv.get("region") else 0.0
    x[o + 3] = 1.0                                                   # bias
    return x

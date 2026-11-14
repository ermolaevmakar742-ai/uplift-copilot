"""ShadowConversationAnalysis: contradiction + momentum + next-best-question
+ deposit path, from one pass of shared signals. JSON-serialisable, no raw text.

Execution order: signals -> momentum -> contradiction (consumes momentum)
-> deposit path -> next-best-question (consumes the three). Each module is
isolated: one failing module yields {"error": <ExcType>} and the rest continue.
"""
import logging
from dataclasses import dataclass, field

from copilot_shadow import contradiction, deposit_graph, momentum, next_question
from copilot_shadow import signals as S

logger = logging.getLogger("copilot_shadow")
ANALYSIS_VERSION = "analysis-1"


@dataclass
class ShadowConversationAnalysis:
    contradiction: dict | None = None
    momentum: dict | None = None
    next_best_question: dict | None = None
    deposit_path: dict | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"contradiction": self.contradiction, "momentum": self.momentum,
                "next_best_question": self.next_best_question,
                "deposit_path": self.deposit_path, "meta": self.meta}


def _safe(name, errors, fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # analytics must never break the shadow record
        logger.exception("SHADOW analysis module failed: %s", name)
        errors[name] = type(e).__name__
        return {"error": type(e).__name__}


def analyze(turns: list, stage: str | None) -> ShadowConversationAnalysis:
    errors: dict = {}
    try:
        fs = S.annotate(turns)
    except Exception as e:
        logger.exception("SHADOW signal extraction failed")
        err = {"error": type(e).__name__}
        return ShadowConversationAnalysis(err, err, err, err, {"version": ANALYSIS_VERSION, "errors": {"signals": type(e).__name__}})
    mom = _safe("momentum", errors, momentum.compute, fs)
    ctr = _safe("contradiction", errors, contradiction.detect, fs, None if "error" in mom else mom)
    path = _safe("deposit_path", errors, deposit_graph.build, fs, stage)
    nbq = _safe("next_best_question", errors, next_question.recommend, fs,
                None if "error" in ctr else ctr, None if "error" in mom else mom,
                None if "error" in path else path)
    meta = {"version": ANALYSIS_VERSION, "turns_used": len(fs),
            "ids_available": any(f.id is not None for f in fs),
            "timestamps_available": any(f.ts is not None for f in fs),
            "errors": errors}
    return ShadowConversationAnalysis(ctr, mom, nbq, path, meta)

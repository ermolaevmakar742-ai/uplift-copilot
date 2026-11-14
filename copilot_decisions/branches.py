"""Decision Records for the non-LLM production response branches (phase 5M): follow-up and fixed-trigger replies.

Same ledger, same contract: a ProductionDecision whose `decision_path` names the branch ("followup" | "fixed_trigger" | "followup_holdout"). Nothing about
the branch changes: the hook only RECORDS, before the existing code sends, and returns the decision_id so the existing send can be linked
to a Response Boundary. No message text is stored or classified; no decision is ever created for a past event; the branch is identified by
the production code path itself, never by guessing.

Fields that do not apply to a branch (regime, model, flow_version, ...) are left UNKNOWN (recorded as NULL with a reason): they are not
filled with look-alike values. Non-blocking and fail-safe like the LLM-path recorder (returns None when the ledger is off or on any error)."""
import logging

from copilot_decisions.recorder import record_production_decision

logger = logging.getLogger("copilot_decisions")

BRANCH_PATHS = ("fixed_trigger", "followup", "followup_holdout")


def record_branch_decision(path: str, *, session_factory, chat_id, message_id=None, trigger=None, client_state=None,
                           extra_metadata: dict | None = None) -> str | None:
    """Returns the decision_id (to link the Response Boundary of the send that follows) or None. Never raises."""
    try:
        if path not in BRANCH_PATHS:
            return None
        fields = {"decision_path": path, "client_state": client_state}
        if trigger:
            fields["trigger"] = trigger
        if extra_metadata:
            fields["extra_metadata"] = extra_metadata
        return record_production_decision(session_factory=session_factory, chat_id=chat_id, message_id=message_id, **fields)
    except Exception:
        logger.exception("branch decision record failed chat=%s path=%s", chat_id, path)
        return None

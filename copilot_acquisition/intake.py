"""First-incoming-message hook. process_message() is SYNCHRONOUS, never raises and returns the text the rest of the pipeline (and the
LLM) may see: every attribution marker is removed BEFORE anything else happens, whether it was valid or not. A valid token schedules
a background bind (only when COPILOT_ACQUISITION_ENABLED); a missing / malformed marker never binds."""
import asyncio
import logging

from copilot_acquisition import contract as C
from copilot_acquisition import store

logger = logging.getLogger("copilot_acquisition")
_tasks: set = set()


RETRY_DELAYS = (5, 10, 20, 30, 60, 60, 120, 120, 180)   # ~10 min total; only while the lead has no client_id yet
_inflight: set = set()                                  # token hashes being bound / retried (a repeated marker never doubles the work)


async def _bind(session_factory, token, chat_id, delays=None):
    key = C.hash_token(token)
    if key in _inflight:
        return
    _inflight.add(key)
    try:
        outcome = await store.bind_token(session_factory, token, chat_id)
        for d in (RETRY_DELAYS if delays is None else delays):      # delayed client_id: bind_token is idempotent, so retrying is safe
            if outcome != "lead_not_ready":
                break
            await asyncio.sleep(d)
            outcome = await store.bind_token(session_factory, token, chat_id)
        logger.info("ACQUISITION bind result=%s", outcome)             # never logs the token or the chat id
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ACQUISITION bind failed")
    finally:
        _inflight.discard(key)


def process_message(*, chat_id, text, session_factory) -> str:
    try:
        token, clean, state = C.parse_message(text)
        if state == "valid" and C.enabled():
            task = asyncio.get_running_loop().create_task(_bind(session_factory, token, chat_id))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        return clean
    except Exception:
        logger.exception("ACQUISITION intake failed")
        try:
            return C.parse_message(text)[1]                             # still strip the marker
        except Exception:
            return text if isinstance(text, str) else ""

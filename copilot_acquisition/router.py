"""GET /go/<token>: public redirect to the Telegram handoff. Disabled (404) unless COPILOT_ACQUISITION_ENABLED.

No open redirect: the destination comes ONLY from the campaign registry and must be an allowlisted Telegram username; the URL is
built here from that username. Invalid / unknown / paused / expired-campaign tokens create no click and answer a plain 404.
Nothing about the visitor (IP, user agent, headers) is read or stored."""
import logging
import time
from collections import deque

from copilot_acquisition import contract as C
from copilot_acquisition import store

logger = logging.getLogger("copilot_acquisition")
MAX_PER_MINUTE = 300
_hits: deque = deque()


def _allowed_now() -> bool:
    now = time.monotonic()
    while _hits and now - _hits[0] > 60:
        _hits.popleft()
    if len(_hits) >= MAX_PER_MINUTE:
        return False
    _hits.append(now)
    return True


def build_router(session_factory):
    from fastapi import APIRouter
    from fastapi.responses import PlainTextResponse, RedirectResponse

    router = APIRouter()

    @router.get("/go/{token}", include_in_schema=False)
    async def go(token: str):
        nf = PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
        try:
            if not C.enabled() or not _allowed_now():
                return nf
            campaign = await store.get_campaign_by_token(session_factory, token)
            if campaign is None or campaign["status"] != "active" or not C.valid_destination(campaign["destination"]):
                return nf
            issued = await store.issue_click(session_factory, campaign)
            return RedirectResponse(issued["url"], status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        except Exception:
            logger.exception("acquisition redirect failed")
            return nf

    return router

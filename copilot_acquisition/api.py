"""Read-only Acquisition Dashboard API (admin only). Every endpoint applies the SAME filters; nothing here writes.

  GET /api/acquisition/summary     KPI cards + funnel + maturity / sample metadata
  GET /api/acquisition/sources     table grouped by source | campaign | creative | medium (drill-down: pass the parent as a filter)
  GET /api/acquisition/timeseries  one metric over time by its own event timestamp
  GET /api/acquisition/filters     available filter values
Existing /api/attribution/* endpoints (and their click revenue) are untouched and not used here."""
import logging
import re
import time

from copilot_acquisition import queries as Q

_LABEL = re.compile(r"^[\w .:+/-]{1,64}$")
_cache: dict = {}
CACHE_TTL = 30.0


def _clean(name, v):
    from fastapi import HTTPException
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not _LABEL.match(v) or "@" in v:
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return v


def _cached(key, ttl=None):
    hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < (CACHE_TTL if ttl is None else ttl) else None


def build_router(require_admin, session_factory):
    from fastapi import APIRouter, Depends, HTTPException, Query

    router = APIRouter()

    def parse(period, source, campaign, creative, medium):
        if period not in Q.PERIODS:
            raise HTTPException(status_code=400, detail="invalid period")
        return period, {"source": _clean("source", source), "campaign": _clean("campaign", campaign),
                        "creative": _clean("creative", creative), "medium": _clean("medium", medium)}

    async def cached(key, fn):
        hit = _cached(key)
        if hit is not None:
            return hit
        try:
            val = await fn()
        except Exception:
            logging.getLogger(__name__).exception("acquisition query failed: %s", key[0])   # the 503 keeps its generic detail; the cause goes to the log
            raise HTTPException(status_code=503, detail="acquisition data unavailable")
        _cache[key] = (time.monotonic(), val)
        return val

    @router.get("/api/acquisition/summary")
    async def summary(period: str = Query("30d"), source: str | None = None, campaign: str | None = None,
                      creative: str | None = None, medium: str | None = None, _=Depends(require_admin)):
        period, f = parse(period, source, campaign, creative, medium)

        async def run():
            (kpi,) = await Q.grouped(session_factory, group_by=None, period=period, filters=f)
            total = await Q.total_clicks(session_factory)
            return {"period": period, "filters": {k: v for k, v in f.items() if v}, "empty": total == 0, "kpi": kpi,
                    "funnel": Q.funnel(kpi),
                    "maturity": {"window_days": Q.MATURITY_DAYS, "immature_clicks": kpi["immature_clicks"], "mature": kpi["mature"]},
                    "sample": {"clicks": kpi["clicks"], "leads": kpi["leads"], "small": kpi["small_sample"]},
                    "definitions": {"cohort": "clicked_at", "qualified": "first funnel transition into offer or a later stage",
                                    "reward": "shadow reward + last delayed correction", "final_30d": "all rewarded decisions past the 30-day window"}}
        return await cached(("summary", period, tuple(sorted(f.items()))), run)

    @router.get("/api/acquisition/sources")
    async def sources(group_by: str = Query("source"), period: str = Query("30d"), source: str | None = None,
                      campaign: str | None = None, creative: str | None = None, medium: str | None = None, _=Depends(require_admin)):
        if group_by not in Q.DIMENSIONS:
            raise HTTPException(status_code=400, detail="invalid group_by")
        period, f = parse(period, source, campaign, creative, medium)

        async def run():
            rows = await Q.grouped(session_factory, group_by=group_by, period=period, filters=f)
            return {"group_by": group_by, "period": period, "parent": {k: v for k, v in f.items() if v}, "rows": rows}
        return await cached(("sources", group_by, period, tuple(sorted(f.items()))), run)

    @router.get("/api/acquisition/timeseries")
    async def timeseries(metric: str = Query("clicks"), period: str = Query("30d"), source: str | None = None,
                         campaign: str | None = None, creative: str | None = None, medium: str | None = None, _=Depends(require_admin)):
        if metric not in ("clicks", "leads", "deposits", "reward"):
            raise HTTPException(status_code=400, detail="invalid metric")
        period, f = parse(period, source, campaign, creative, medium)
        return await cached(("ts", metric, period, tuple(sorted(f.items()))),
                            lambda: Q.timeseries(session_factory, metric=metric, period=period, filters=f))

    @router.get("/api/acquisition/filters")
    async def filters(_=Depends(require_admin)):
        return await cached(("filters",), lambda: Q.filter_options(session_factory))

    return router

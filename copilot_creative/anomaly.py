"""Explainable anomaly score (0-100): how unusual a video's performance is relative to its own context. It is a RANKING SIGNAL built only from
stored measurements — not a probability of virality and not a forecast of future views. Every component reports its raw value, its normalised
value and its weight; components whose inputs are missing are omitted and the remaining weights are renormalised (shown in `missing`).

  relative_views  views / median(author's other videos)   [fallback: views / author followers]     weight 0.40
  engagement      (likes + comments + shares) / views                                              weight 0.20
  freshness       exp(-age_days / 14)                                                              weight 0.15
  velocity        views gained per day between the first and last stored measurements,
                  relative to the author's median (fallback: share of current views)  — needs >= 2 measurements >= 1h apart   weight 0.15
  repeatability   distinct authors in the library with the same video_type + niche in the last 30 days  — needs a type+niche   weight 0.10
"""
import math
import statistics
from datetime import datetime, timezone

WEIGHTS = {"relative_views": 0.40, "engagement": 0.20, "freshness": 0.15, "velocity": 0.15, "repeatability": 0.10}
DISCLAIMER = "Relative ranking signal from stored measurements. Not a probability of virality and not a guarantee of future views."


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def compute(*, published_at, metrics: list[dict], peer_views: list[int] | None, repeat_authors: int | None = None, now: datetime | None = None) -> dict:
    """metrics: snapshots sorted ascending by captured_at, each {captured_at, views, likes, comments, shares, followers} (None = unknown)."""
    now = _aware(now) or datetime.now(timezone.utc)
    last = metrics[-1] if metrics else {}
    views, followers = last.get("views"), next((m.get("followers") for m in reversed(metrics) if m.get("followers") is not None), None)
    comps, missing = {}, {}
    peers = [v for v in (peer_views or []) if isinstance(v, int) and v >= 0]

    if views is not None and len(peers) >= 3:
        med = statistics.median(peers)
        r = views / max(med, 1)
        comps["relative_views"] = {"raw": round(r, 2), "norm": min(1.0, math.log2(max(r, 1)) / math.log2(20)),
                                   "basis": f"{views:,} views vs author median {int(med):,} over {len(peers)} other videos"}
    elif views is not None and followers:
        r = views / max(followers, 1)
        comps["relative_views"] = {"raw": round(r, 2), "norm": min(1.0, math.log2(max(r, 1)) / math.log2(5)),
                                   "basis": f"{views:,} views vs {followers:,} followers (no peer sample for the author)"}
    else:
        missing["relative_views"] = "needs views plus either >=3 other videos of the author or the follower count"

    if views and all(last.get(k) is not None for k in ("likes",)) :
        inter = sum(last.get(k) or 0 for k in ("likes", "comments", "shares"))
        er = inter / views
        comps["engagement"] = {"raw": round(er, 4), "norm": min(1.0, er / 0.15),
                               "basis": f"{inter:,} interactions / {views:,} views (only the counters the source provides)"}
    else:
        missing["engagement"] = "needs views and at least the like count"

    pub = _aware(published_at)
    if pub:
        age = max(0.0, (now - pub).total_seconds() / 86400)
        comps["freshness"] = {"raw": round(age, 1), "norm": math.exp(-age / 14), "basis": f"published {age:.1f} days ago"}
    else:
        missing["freshness"] = "publication date unknown"

    pts = [(_aware(m["captured_at"]), m["views"]) for m in metrics if m.get("views") is not None]
    if len(pts) >= 2 and (pts[-1][0] - pts[0][0]).total_seconds() >= 3600 and pts[-1][1] >= pts[0][1]:
        days = (pts[-1][0] - pts[0][0]).total_seconds() / 86400
        per_day = (pts[-1][1] - pts[0][1]) / days
        if len(peers) >= 3:
            rel = per_day / max(statistics.median(peers), 1)
            norm, basis = min(1.0, rel), f"+{int(per_day):,} views/day over {days:.1f} days ({rel:.2f}x author median per day)"
        else:
            share = (pts[-1][1] - pts[0][1]) / max(pts[-1][1], 1)
            norm, basis = min(1.0, share / 0.5), f"+{int(per_day):,} views/day over {days:.1f} days ({share:.0%} of current views)"
        comps["velocity"] = {"raw": round(per_day, 1), "norm": norm, "basis": basis}
    else:
        missing["velocity"] = "needs two view measurements at least 1 hour apart (only %d stored)" % len(pts)

    if repeat_authors is not None:
        comps["repeatability"] = {"raw": repeat_authors, "norm": min(1.0, max(0, repeat_authors - 1) / 4),
                                  "basis": f"{repeat_authors} distinct author(s) in the library use this video type in this niche (30 d)"}
    else:
        missing["repeatability"] = "needs video type and niche on the reference"

    if not comps or ("relative_views" not in comps and "velocity" not in comps and "engagement" not in comps):
        return {"score": None, "components": comps, "missing": missing, "disclaimer": DISCLAIMER,
                "note": "Not enough measurements for a meaningful score" if not comps else "Only freshness/repeatability available: score withheld"}
    wsum = sum(WEIGHTS[k] for k in comps)
    for k, c in comps.items():
        c["weight"] = round(WEIGHTS[k] / wsum, 3)
        c["norm"] = round(c["norm"], 3)
    score = round(100 * sum(c["weight"] * c["norm"] for c in comps.values()), 1)
    return {"score": score, "components": comps, "missing": missing, "disclaimer": DISCLAIMER}

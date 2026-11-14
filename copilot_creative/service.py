"""Persistence + read models for Creative Intelligence. Plain SQLAlchemy Core on the injected session factory (same DB as the rest of COPILOT).
Filtering/sorting of references is done in Python over the (small, human-curated) table so the anomaly score can use the stored measurements."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, update

from copilot_creative import anomaly, store as S
from copilot_creative.sources.base import VideoRecord, parse_url, url_key

TREND_STATUSES = ("new", "watching", "rising", "saturated", "dead")
PRODUCTION_STATUSES = ("idea", "analysed", "in_production", "published", "archived")
VIDEO_TYPES = ("talking_head", "pov", "reaction", "dance", "transition", "cinematic", "storytelling", "prank", "challenge", "before_after",
               "voice_over", "ugc_style", "green_screen", "transformation", "short_sketch")
DELIVERY = ("direct_to_camera", "voice_over", "text_on_screen", "music_only", "dialogue", "no_face", "screen_recording", "mixed")
METRIC_KEYS = ("views", "likes", "comments", "shares", "followers")


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d):
    d = aware(d)
    return d.isoformat() if d else None


def jl(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _nint(v):
    if v is None or v == "":
        return None
    try:
        n = int(float(str(v).replace(",", "").replace(" ", "")))
    except ValueError:
        raise ValueError(f"not a number: {v!r}")
    if n < 0:
        raise ValueError("negative metric")
    return n


class Service:
    def __init__(self, session_factory):
        self.sf = session_factory
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return
        bind = self.sf.kw.get("bind")
        async with bind.begin() as conn:
            await conn.run_sync(S.metadata.create_all)
        # a restart interrupts running jobs: surface them as failed (retryable) instead of "processing" forever
        async with self.sf() as db:
            for t in (S.cr_analyses, S.cr_prompts):
                await db.execute(update(t).where(t.c.status.in_(("processing", "queued"))).values(status="failed", error="Interrupted by a service restart; press Retry", updated_at=now()))
            await db.commit()
        self._ready = True

    # ---------------- references ----------------
    async def upsert_reference(self, rec: VideoRecord | None = None, *, url: str | None = None, file_key: str | None = None, platform: str | None = None,
                               fields: dict | None = None, save: bool = False, snapshot: bool = True) -> tuple[int, bool]:
        """Create or update a reference (identity = canonical URL, or the uploaded file's hash). Returns (id, created)."""
        fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
        if rec is not None:
            _, canon, ext = parse_url(rec.url)
            key = url_key(canon)
            base = dict(platform=rec.platform, url=canon, external_id=rec.external_id or ext, title=rec.title, author=rec.author, author_url=rec.author_url,
                        thumbnail_url=rec.thumbnail_url, published_at=rec.published_at, duration_s=rec.duration_s, source=rec.source)
            if rec.language and "language" not in fields:
                fields["language"] = rec.language
            if rec.peer_views:
                base["peer_views"] = json.dumps({"views": rec.peer_views[:50], "captured_at": now().isoformat(), "source": rec.source})
        elif url:
            plat, canon, ext = parse_url(url)
            key = url_key(canon)
            base = dict(platform=platform or plat, url=canon, external_id=ext, source="manual")
        else:
            key = file_key or ""
            base = dict(platform=platform or "other", source="upload")
        base = {k: v for k, v in base.items() if v is not None}
        async with self.sf() as db:
            row = (await db.execute(select(S.cr_references.c.id, S.cr_references.c.saved).where(S.cr_references.c.url_key == key))).first()
            created = row is None
            if created:
                vals = {**base, **fields, "url_key": key, "saved": bool(save), "discovered_at": now(), "updated_at": now()}
                vals.setdefault("platform", "other")
                rid = (await db.execute(insert(S.cr_references).values(**vals).returning(S.cr_references.c.id))).scalar_one()
            else:
                rid = row[0]
                upd = {k: v for k, v in {**base, **fields}.items() if k != "source" or base.get("source") not in ("manual", "upload")}
                upd["updated_at"] = now()
                if save and not row[1]:
                    upd["saved"] = True
                await db.execute(update(S.cr_references).where(S.cr_references.c.id == rid).values(**upd))
            if rec is not None and snapshot and any(getattr(rec, k) is not None for k in METRIC_KEYS):
                await self._snapshot(db, rid, {k: getattr(rec, k) for k in METRIC_KEYS}, rec.source, "source")
            await db.commit()
        return rid, created

    async def _snapshot(self, db, rid: int, m: dict, source: str, kind: str, when: datetime | None = None) -> bool:
        when = when or now()
        last = (await db.execute(select(S.cr_metrics).where(S.cr_metrics.c.ref_id == rid).order_by(S.cr_metrics.c.captured_at.desc()).limit(1))).mappings().first()
        if last and kind == "source" and all(last[k] == m.get(k) for k in METRIC_KEYS) and (when - aware(last["captured_at"])) < timedelta(hours=1):
            return False                                                    # identical re-read within the hour: not a new measurement
        await db.execute(insert(S.cr_metrics).values(ref_id=rid, captured_at=when, source=source, kind=kind, **{k: m.get(k) for k in METRIC_KEYS}))
        return True

    async def add_manual_metric(self, rid: int, m: dict, captured_at: datetime | None = None) -> None:
        vals = {k: _nint(m.get(k)) for k in METRIC_KEYS}
        if all(v is None for v in vals.values()):
            raise ValueError("enter at least one metric")
        async with self.sf() as db:
            if not (await db.execute(select(S.cr_references.c.id).where(S.cr_references.c.id == rid))).first():
                raise KeyError(rid)
            await self._snapshot(db, rid, vals, "manual", "manual", captured_at)
            await db.execute(update(S.cr_references).where(S.cr_references.c.id == rid).values(updated_at=now()))
            await db.commit()

    def _view(self, r: dict, metrics: list[dict], repeat: dict) -> dict:
        peers = jl(r.get("peer_views"), {})
        key = (r.get("video_type"), (r.get("niche") or "").lower())
        rep = repeat.get(key) if key[0] and key[1] else None
        ms = [{**{k: m[k] for k in METRIC_KEYS}, "captured_at": aware(m["captured_at"]), "source": m["source"], "kind": m["kind"]} for m in metrics]
        an = anomaly.compute(published_at=r.get("published_at"), metrics=ms, peer_views=peers.get("views"), repeat_authors=rep)
        last = ms[-1] if ms else {}
        er = None
        if last.get("views") and last.get("likes") is not None:
            er = round(sum(last.get(k) or 0 for k in ("likes", "comments", "shares")) / last["views"], 4)
        return {"id": r["id"], "platform": r["platform"], "url": r["url"], "title": r["title"], "author": r["author"], "author_url": r["author_url"],
                "thumbnail_url": r["thumbnail_url"], "published_at": iso(r["published_at"]), "duration_s": r["duration_s"], "niche": r["niche"], "geo": r["geo"],
                "language": r["language"], "keywords": r["keywords"], "video_type": r["video_type"], "delivery": r["delivery"], "trend_status": r["trend_status"],
                "production_status": r["production_status"], "tags": jl(r["tags"], []), "notes": r["notes"], "source": r["source"], "saved": bool(r["saved"]),
                "discovered_at": iso(r["discovered_at"]), "updated_at": iso(r["updated_at"]), "has_peer_sample": bool(peers.get("views")),
                "metrics": {k: last.get(k) for k in METRIC_KEYS}, "metrics_updated_at": iso(last.get("captured_at")) if last else None,
                "measurements": len(ms), "engagement_rate": er, "anomaly": an}

    async def _repeat_map(self, db) -> dict:
        since = now() - timedelta(days=30)
        rows = (await db.execute(select(S.cr_references.c.video_type, S.cr_references.c.niche, S.cr_references.c.author)
                                 .where(S.cr_references.c.video_type.is_not(None), S.cr_references.c.niche.is_not(None), S.cr_references.c.discovered_at >= since))).all()
        out: dict = {}
        for vt, ni, au in rows:
            out.setdefault((vt, (ni or "").lower()), set()).add(au or "?")
        return {k: len(v) for k, v in out.items()}

    async def list_refs(self, f: dict) -> dict:
        async with self.sf() as db:
            q = select(S.cr_references)
            if f.get("saved") is not None:
                q = q.where(S.cr_references.c.saved == bool(f["saved"]))
            for col in ("platform", "geo", "language", "video_type", "delivery", "trend_status", "production_status", "source"):
                if f.get(col):
                    q = q.where(getattr(S.cr_references.c, col) == f[col])
            if f.get("niche"):
                q = q.where(func.lower(S.cr_references.c.niche).like(f"%{f['niche'].lower()}%"))
            if f.get("q"):
                like = f"%{f['q'].lower()}%"
                c = S.cr_references.c
                q = q.where(func.lower(func.coalesce(c.title, "") + " " + func.coalesce(c.author, "") + " " + func.coalesce(c.keywords, "") + " " + func.coalesce(c.tags, "") + " " + func.coalesce(c.niche, "") + " " + func.coalesce(c.notes, "")).like(like))
            if f.get("published_within_days"):
                q = q.where(S.cr_references.c.published_at >= now() - timedelta(days=int(f["published_within_days"])))
            if f.get("discovered_within_days"):
                q = q.where(S.cr_references.c.discovered_at >= now() - timedelta(days=int(f["discovered_within_days"])))
            if f.get("max_duration"):
                q = q.where(S.cr_references.c.duration_s <= float(f["max_duration"]))
            rows = [dict(r) for r in (await db.execute(q.limit(5000))).mappings().all()]
            ids = [r["id"] for r in rows]
            mm: dict = {}
            if ids:
                for m in (await db.execute(select(S.cr_metrics).where(S.cr_metrics.c.ref_id.in_(ids)).order_by(S.cr_metrics.c.captured_at))).mappings().all():
                    mm.setdefault(m["ref_id"], []).append(dict(m))
            repeat = await self._repeat_map(db)
        items = [self._view(r, mm.get(r["id"], []), repeat) for r in rows]

        def rng(it, key, lo, hi):
            v = it["metrics"].get(key)
            return not ((lo is not None and (v is None or v < lo)) or (hi is not None and (v is None or v > hi)))
        items = [i for i in items if rng(i, "views", f.get("min_views"), f.get("max_views")) and rng(i, "followers", f.get("min_followers"), f.get("max_followers"))]
        sort = f.get("sort") or "anomaly"
        keyf = {"anomaly": lambda i: (i["anomaly"]["score"] is not None, i["anomaly"]["score"] or 0),
                "views": lambda i: (i["metrics"]["views"] is not None, i["metrics"]["views"] or 0),
                "engagement": lambda i: (i["engagement_rate"] is not None, i["engagement_rate"] or 0),
                "date": lambda i: (i["published_at"] is not None, i["published_at"] or ""),
                "discovered": lambda i: (True, i["discovered_at"] or "")}.get(sort)
        if keyf:
            items.sort(key=keyf, reverse=True)
        total = len(items)
        off, lim = max(0, int(f.get("offset") or 0)), min(200, max(1, int(f.get("limit") or 48)))
        return {"total": total, "items": items[off:off + lim]}

    async def get_ref(self, rid: int, include_reports: bool = True) -> dict | None:
        async with self.sf() as db:
            r = (await db.execute(select(S.cr_references).where(S.cr_references.c.id == rid))).mappings().first()
            if not r:
                return None
            ms = [dict(m) for m in (await db.execute(select(S.cr_metrics).where(S.cr_metrics.c.ref_id == rid).order_by(S.cr_metrics.c.captured_at))).mappings().all()]
            repeat = await self._repeat_map(db)
            view = self._view(dict(r), ms, repeat)
            view["metric_history"] = [{"captured_at": iso(m["captured_at"]), "source": m["source"], "kind": m["kind"], **{k: m[k] for k in METRIC_KEYS}} for m in ms]
            peers = jl(r["peer_views"], {})
            view["peer_sample"] = {"count": len(peers.get("views", [])), "captured_at": peers.get("captured_at"), "source": peers.get("source")} if peers else None
            view["analyses"] = [self._analysis_view(a, include_reports) for a in (await db.execute(select(S.cr_analyses).where(S.cr_analyses.c.ref_id == rid).order_by(S.cr_analyses.c.id.desc()))).mappings().all()]
            view["prompts"] = [self._prompt_view(p, include_reports) for p in (await db.execute(select(S.cr_prompts).where(S.cr_prompts.c.ref_id == rid).order_by(S.cr_prompts.c.id.desc()))).mappings().all()]
            view["publications"] = [{**{k: p[k] for k in ("id", "url", "platform", "views", "likes", "comments", "shares", "notes")}, "posted_at": iso(p["posted_at"]), "created_at": iso(p["created_at"])}
                                    for p in (await db.execute(select(S.cr_publications).where(S.cr_publications.c.ref_id == rid).order_by(S.cr_publications.c.id.desc()))).mappings().all()]
        return view

    EDITABLE = {"niche": 120, "geo": 16, "language": 16, "video_type": 40, "delivery": 40, "trend_status": 24, "production_status": 24, "notes": 20000, "title": 500, "author": 200}

    async def update_ref(self, rid: int, patch: dict) -> bool:
        vals: dict = {}
        for k, mx in self.EDITABLE.items():
            if k in patch:
                v = patch[k]
                v = None if v in (None, "") else str(v).strip()[:mx]
                if k == "trend_status" and v not in TREND_STATUSES:
                    raise ValueError("invalid trend_status")
                if k == "production_status" and v not in PRODUCTION_STATUSES:
                    raise ValueError("invalid production_status")
                vals[k] = v
        if "tags" in patch:
            tags = patch["tags"]
            if not isinstance(tags, list):
                raise ValueError("tags must be a list")
            vals["tags"] = json.dumps(list(dict.fromkeys(str(t).strip()[:40] for t in tags if str(t).strip()))[:30], ensure_ascii=False)
        if "saved" in patch:
            vals["saved"] = bool(patch["saved"])
        if not vals:
            raise ValueError("nothing to update")
        vals["updated_at"] = now()
        async with self.sf() as db:
            res = await db.execute(update(S.cr_references).where(S.cr_references.c.id == rid).values(**vals))
            await db.commit()
            return res.rowcount > 0

    async def delete_ref(self, rid: int) -> bool:
        async with self.sf() as db:
            for t in (S.cr_metrics, S.cr_analyses, S.cr_prompts, S.cr_publications):      # explicit: SQLite FKs are off by default
                await db.execute(delete(t).where(t.c.ref_id == rid))
            res = await db.execute(delete(S.cr_references).where(S.cr_references.c.id == rid))
            await db.commit()
            return res.rowcount > 0

    async def add_publication(self, rid: int, d: dict) -> int:
        vals = {k: _nint(d.get(k)) for k in ("views", "likes", "comments", "shares")}
        posted = None
        if d.get("posted_at"):
            posted = aware(datetime.fromisoformat(str(d["posted_at"]).replace("Z", "+00:00")))
        async with self.sf() as db:
            if not (await db.execute(select(S.cr_references.c.id).where(S.cr_references.c.id == rid))).first():
                raise KeyError(rid)
            pid = (await db.execute(insert(S.cr_publications).values(ref_id=rid, url=(d.get("url") or None), platform=(d.get("platform") or None), posted_at=posted,
                                                                      notes=(str(d.get("notes") or "")[:5000] or None), **vals).returning(S.cr_publications.c.id))).scalar_one()
            await db.commit()
        return pid

    # ---------------- analyses ----------------
    def _analysis_view(self, a, full=True) -> dict:
        v = {"id": a["id"], "ref_id": a["ref_id"], "status": a["status"], "error": a["error"], "model": a["model"], "created_at": iso(a["created_at"]), "updated_at": iso(a["updated_at"]),
             "input_sha256": a["input_sha256"][:12], "can_retry": a["status"] == "failed" and bool(a["upload_path"]), "frames": jl(a["frames"], [])}
        if full:
            v.update(facts=jl(a["facts"], None), report=jl(a["report"], None), log=jl(a["log"], []), usage=jl(a["usage"], None))
        return v

    async def create_analysis(self, ref_id: int, sha: str, upload_path: str) -> tuple[int, bool]:
        """(analysis_id, reused). A completed analysis of the same file for this reference is reused: no new paid processing."""
        async with self.sf() as db:
            done = (await db.execute(select(S.cr_analyses.c.id).where(S.cr_analyses.c.ref_id == ref_id, S.cr_analyses.c.input_sha256 == sha, S.cr_analyses.c.status == "completed").limit(1))).first()
            if done:
                return done[0], True
            live = (await db.execute(select(S.cr_analyses.c.id).where(S.cr_analyses.c.ref_id == ref_id, S.cr_analyses.c.input_sha256 == sha, S.cr_analyses.c.status.in_(("queued", "processing"))).limit(1))).first()
            if live:
                return live[0], True
            failed = (await db.execute(select(S.cr_analyses.c.id).where(S.cr_analyses.c.ref_id == ref_id, S.cr_analyses.c.input_sha256 == sha, S.cr_analyses.c.status == "failed").limit(1))).first()
            if failed:                                                      # same file again after a failure: reuse the row, no duplicate
                await db.execute(update(S.cr_analyses).where(S.cr_analyses.c.id == failed[0]).values(status="queued", error=None, upload_path=upload_path, updated_at=now()))
                await db.commit()
                return failed[0], False
            aid = (await db.execute(insert(S.cr_analyses).values(ref_id=ref_id, status="queued", input_sha256=sha, upload_path=upload_path).returning(S.cr_analyses.c.id))).scalar_one()
            await db.commit()
            return aid, False

    async def get_analysis(self, aid: int):
        async with self.sf() as db:
            return (await db.execute(select(S.cr_analyses).where(S.cr_analyses.c.id == aid))).mappings().first()

    async def analysis_view(self, aid: int) -> dict | None:
        a = await self.get_analysis(aid)
        return self._analysis_view(a) if a else None

    async def set_analysis(self, aid: int, **vals) -> None:
        for k in ("frames", "facts", "report", "usage"):
            if k in vals and not isinstance(vals[k], str) and vals[k] is not None:
                vals[k] = json.dumps(vals[k], ensure_ascii=False)
        async with self.sf() as db:
            await db.execute(update(S.cr_analyses).where(S.cr_analyses.c.id == aid).values(updated_at=now(), **vals))
            await db.commit()

    async def log_analysis(self, aid: int, msg: str) -> None:
        async with self.sf() as db:
            cur = (await db.execute(select(S.cr_analyses.c.log).where(S.cr_analyses.c.id == aid))).scalar_one_or_none()
            lg = jl(cur, [])[-60:] + [{"t": now().strftime("%H:%M:%S"), "msg": str(msg)[:300]}]
            await db.execute(update(S.cr_analyses).where(S.cr_analyses.c.id == aid).values(log=json.dumps(lg, ensure_ascii=False), updated_at=now()))
            await db.commit()

    async def pending_uploads(self) -> set[str]:
        async with self.sf() as db:
            return {p for (p,) in (await db.execute(select(S.cr_analyses.c.upload_path).where(S.cr_analyses.c.upload_path.is_not(None)))).all()}

    async def latest_completed_analysis(self, rid: int):
        async with self.sf() as db:
            return (await db.execute(select(S.cr_analyses).where(S.cr_analyses.c.ref_id == rid, S.cr_analyses.c.status == "completed").order_by(S.cr_analyses.c.id.desc()).limit(1))).mappings().first()

    # ---------------- prompts ----------------
    def _prompt_view(self, p, full=True) -> dict:
        v = {"id": p["id"], "ref_id": p["ref_id"], "analysis_id": p["analysis_id"], "status": p["status"], "error": p["error"], "model": p["model"],
             "created_at": iso(p["created_at"]), "params": jl(p["params"], {})}
        if full:
            v["result"] = jl(p["result"], None)
            v["usage"] = jl(p["usage"], None)
        return v

    @staticmethod
    def params_hash(params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def create_prompt(self, ref_id: int, analysis_id: int, params: dict, regenerate: bool) -> tuple[int, bool]:
        h = self.params_hash(params)
        async with self.sf() as db:
            if not regenerate:
                hit = (await db.execute(select(S.cr_prompts.c.id).where(S.cr_prompts.c.ref_id == ref_id, S.cr_prompts.c.analysis_id == analysis_id, S.cr_prompts.c.params_hash == h,
                                                                        S.cr_prompts.c.status.in_(("completed", "queued", "processing"))).order_by(S.cr_prompts.c.id.desc()).limit(1))).first()
                if hit:
                    return hit[0], True
            pid = (await db.execute(insert(S.cr_prompts).values(ref_id=ref_id, analysis_id=analysis_id, status="queued", params=json.dumps(params, ensure_ascii=False), params_hash=h)
                                    .returning(S.cr_prompts.c.id))).scalar_one()
            await db.commit()
            return pid, False

    async def get_prompt(self, pid: int):
        async with self.sf() as db:
            return (await db.execute(select(S.cr_prompts).where(S.cr_prompts.c.id == pid))).mappings().first()

    async def prompt_view(self, pid: int) -> dict | None:
        p = await self.get_prompt(pid)
        return self._prompt_view(p) if p else None

    async def set_prompt(self, pid: int, **vals) -> None:
        for k in ("result", "usage"):
            if k in vals and vals[k] is not None and not isinstance(vals[k], str):
                vals[k] = json.dumps(vals[k], ensure_ascii=False)
        async with self.sf() as db:
            await db.execute(update(S.cr_prompts).where(S.cr_prompts.c.id == pid).values(updated_at=now(), **vals))
            await db.commit()

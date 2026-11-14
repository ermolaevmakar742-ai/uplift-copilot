"""Creative Intelligence HTTP API (admin panel only). `build_router(require_admin, session_factory, audit_log, env=None, llm=None, sources=None)`.
Authentication/audit are injected; this module never imports main.py. Every /api/creative route depends on `require_admin`.

  GET  /api/creative/status | /sources | /presets
  POST /api/creative/search                      run a source adapter (stores results as "discovered" with a metric snapshot)
  GET  /api/creative/refs                        Trend Radar / Library listing (filters, anomaly sort)      POST /refs add by URL    POST /refs/import  JSON|CSV
  GET|PATCH|DELETE /api/creative/refs/{id}       POST /refs/{id}/metrics (manual measurement)  /refresh (re-read source)  /publications
  POST /api/creative/upload                      multipart video (+ optional ref_id) -> analysis job
  GET  /api/creative/analyses/{id}               POST /analyses/{id}/retry     GET /analyses/{id}/frames/{file}
  POST /api/creative/forge                       Prompt Forge job              GET /prompts/{id}
  GET  /api/creative/refs/{id}/export?format=md|json
Static UI assets are served without auth at /creative-assets/* (no data, no secrets)."""
import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response

from copilot_creative import analysis, config, export, forge, media
from copilot_creative.llm import LLM
from copilot_creative.service import DELIVERY, PRODUCTION_STATUSES, TREND_STATUSES, VIDEO_TYPES, Service, _nint
from copilot_creative.sources import SearchParams, SourceError, VideoRecord, default_registry, parse_url

logger = logging.getLogger("copilot_creative")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
MAX_JSON = 2_000_000
ERR_STATUS = {"needs_key": 409, "bad_key": 502, "quota_or_forbidden": 502, "source_unreachable": 502, "source_error": 502, "video_unavailable": 404,
              "invalid_url": 400, "query_required": 400, "search_unsupported": 400}


def err(status: int, code: str, message: str = "", **extra):
    return HTTPException(status, detail={"ok": False, "error": code, "message": message or code, **extra})


def _dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid date: {s!r}")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def build_router(require_admin, session_factory, audit_log=None, env=None, llm: LLM | None = None, sources=None) -> APIRouter:
    envmap = os.environ if env is None else env
    cfg = config.load(envmap)
    svc = Service(session_factory)
    llm = llm or LLM(envmap)
    reg = sources or default_registry(envmap)
    router = APIRouter(prefix="/api/creative", tags=["creative"], dependencies=[Depends(require_admin)])
    assets = APIRouter(tags=["creative-assets"])
    sem = asyncio.Semaphore(1)                                   # one video analysis at a time (small server)
    tasks: set = set()

    async def audit(action: str, request: Request, details: str) -> None:
        if audit_log is not None:
            try:
                await audit_log(action, ip=request.client.host if request.client else None, details=details[:480])
            except Exception:
                logger.warning("creative audit failed action=%s", action)

    def spawn(coro):
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_JSON:
            raise err(413, "body_too_large")
        try:
            import json
            d = json.loads(raw or b"{}")
        except ValueError:
            raise err(400, "invalid_json")
        if not isinstance(d, dict):
            raise err(400, "invalid_json", "JSON object expected")
        return d

    async def ready():
        try:
            await svc.ensure()
        except Exception:
            logger.exception("creative schema unavailable")
            raise err(503, "creative_unavailable", "Creative Intelligence storage is unavailable")

    def src_err(e: SourceError):
        return err(ERR_STATUS.get(e.code, 502), e.code, e.message)

    # ------------------------------------------------------------------ status
    @router.get("/status")
    async def status():
        await ready()
        ff = media.ffmpeg_path()
        mods, how = await llm.modalities(cfg.vision_model) if llm.configured else (None, "llm_not_configured")
        return {"ok": True, "ffmpeg": {"available": bool(ff)},
                "vision": {"model": cfg.vision_model, "image_input": bool(mods and "image" in mods), "verified_by": how, "llm_configured": llm.configured},
                "text_model": cfg.text_model,
                "transcription": {"available": bool(cfg.stt_url and envmap.get("COPILOT_CREATIVE_STT_KEY")), "note": "" if cfg.stt_url else "No speech-to-text provider configured: transcripts are reported as unavailable."},
                "limits": {"max_upload_mb": cfg.max_upload_mb, "max_duration_s": cfg.max_duration_s, "max_frames": cfg.max_frames, "formats": sorted(config.ALLOWED_EXT)},
                "sources": reg.statuses(), "coverage": reg.coverage(),
                "enums": {"video_types": VIDEO_TYPES, "delivery": DELIVERY, "trend_statuses": TREND_STATUSES, "production_statuses": PRODUCTION_STATUSES}}

    @router.get("/sources")
    async def sources_list():
        return {"ok": True, "sources": reg.statuses()}

    @router.get("/presets")
    async def presets():
        return {"ok": True, "presets": {k: {kk: vv for kk, vv in v.items() if kk != "style"} for k, v in forge.PRESETS.items()}, "goals": forge.GOALS, "aspects": forge.ASPECTS}

    # ------------------------------------------------------------------ search / add / import
    @router.post("/search")
    async def search(request: Request):
        await ready()
        d = await body(request)
        try:
            p = SearchParams(platform=str(d.get("platform") or "youtube"), query=str(d.get("query") or "")[:200], geo=(d.get("geo") or None), language=(d.get("language") or None),
                             published_within_days=int(d["published_within_days"]) if d.get("published_within_days") else None,
                             max_duration_s=int(d["max_duration_s"]) if d.get("max_duration_s") else None, order=d.get("order") if d.get("order") in ("views", "date") else "views",
                             limit=min(50, max(1, int(d.get("limit") or 25))))
        except (TypeError, ValueError):
            raise err(400, "invalid_params")
        adapter = reg.get(d["source"]) if d.get("source") else next(iter(reg.for_platform(p.platform, "search")), None)
        if adapter is None or p.platform not in adapter.platforms or "search" not in adapter.status().capabilities:
            raise err(409, "no_search_source", f"No connected search source for {p.platform}. Connect one (see Sources) or add references by URL / import.",
                      sources=[s for s in reg.statuses() if p.platform in s["platforms"]])
        st = adapter.status()
        if st.state == "needs_key":
            raise err(409, "needs_key", f"{st.name} requires {st.requires}", requires=st.requires, hint=st.note)
        try:
            recs = await adapter.search(p)
            reg.note_ok(adapter.id)
        except SourceError as e:
            reg.note_error(adapter.id, e.code, e.message)
            raise src_err(e)
        ids, created = [], 0
        for r in recs:
            rid, c = await svc.upsert_reference(r, fields={"niche": (d.get("niche") or "")[:120], "geo": (p.geo or "")[:16], "keywords": p.query,
                                                           "language": None if r.language else (p.language or "")})
            ids.append(rid)
            created += c
        await audit("creative_search", request, f"CREATIVE_SEARCH source={adapter.id} q={p.query[:60]!r} found={len(recs)} new={created}")
        return {"ok": True, "source": adapter.id, "found": len(recs), "new": created, "ids": ids}

    async def resolve(url: str) -> tuple[VideoRecord | None, list[str]]:
        platform, canon, ext = parse_url(url)
        warnings, rec = [], None
        for a in [reg.get("youtube_api"), reg.get("oembed")]:
            if a is None or platform not in a.platforms or a.status().state not in ("ready", "metadata_only"):
                continue
            try:
                rec = await a.resolve_url(url)
                if rec:
                    reg.note_ok(a.id)
            except SourceError as e:
                if e.code != "video_unavailable":                # a private/removed video is not a source outage
                    reg.note_error(a.id, e.code, e.message)
                warnings.append(f"{a.name}: {e.message}")
                continue
            if rec:
                break
        if rec is None:
            warnings.append("Metadata could not be fetched automatically; the reference was saved with the URL only. Fill in details by hand or upload the video.")
        return rec, warnings

    @router.post("/refs")
    async def add_ref(request: Request):
        await ready()
        d = await body(request)
        try:
            rec, warnings = await resolve(str(d.get("url") or ""))
        except SourceError as e:
            raise src_err(e)
        fields = {k: str(d.get(k) or "").strip()[:120] for k in ("niche", "geo", "language", "video_type", "delivery") if d.get(k)}
        if d.get("title"):
            fields["title"] = str(d["title"])[:500]
        rid, created = await svc.upsert_reference(rec, url=None if rec else d["url"], fields=fields, save=d.get("save", True))
        if d.get("metrics") and isinstance(d["metrics"], dict):
            try:
                await svc.add_manual_metric(rid, d["metrics"])
            except ValueError as e:
                warnings.append(f"Manual metrics ignored: {e}")
        await audit("creative_ref_added", request, f"CREATIVE_REF_ADDED id={rid} created={created}")
        return {"ok": True, "id": rid, "created": created, "warnings": warnings, "ref": await svc.get_ref(rid, include_reports=False)}

    @router.post("/refs/import")
    async def import_refs(request: Request):
        await ready()
        d = await body(request)
        rows = d.get("records")
        if isinstance(d.get("csv"), str):
            rows = list(csv.DictReader(io.StringIO(d["csv"][:MAX_JSON])))
        if not isinstance(rows, list) or not rows:
            raise err(400, "no_records", "Provide records:[...] or csv:'url,title,views,...'")
        ok, errors = 0, []
        for i, row in enumerate(rows[:500], 1):
            try:
                row = {k.strip().lower(): v for k, v in row.items() if isinstance(k, str)}
                plat, canon, ext = parse_url(str(row.get("url") or ""))
                rec = VideoRecord(platform=row.get("platform") or plat, url=canon, source="import", external_id=ext, title=(row.get("title") or None), author=(row.get("author") or None),
                                  published_at=_dt(row.get("published_at")), duration_s=float(row["duration_s"]) if row.get("duration_s") not in (None, "") else None,
                                  views=_nint(row.get("views")), likes=_nint(row.get("likes")), comments=_nint(row.get("comments")), shares=_nint(row.get("shares")),
                                  followers=_nint(row.get("followers")))
                await svc.upsert_reference(rec, fields={k: str(row[k])[:120] for k in ("niche", "geo", "language", "video_type", "delivery") if row.get(k)}, save=bool(d.get("save", False)))
                ok += 1
            except (ValueError, SourceError, TypeError) as e:
                errors.append({"row": i, "error": str(e)[:160]})
        await audit("creative_import", request, f"CREATIVE_IMPORT ok={ok} errors={len(errors)}")
        return {"ok": True, "imported": ok, "errors": errors}

    # ------------------------------------------------------------------ library
    @router.get("/refs")
    async def list_refs(request: Request):
        await ready()
        q = dict(request.query_params)
        f = {k: q[k] for k in ("platform", "niche", "geo", "language", "video_type", "delivery", "trend_status", "production_status", "q", "sort", "source") if q.get(k)}
        try:
            for k in ("published_within_days", "discovered_within_days", "max_duration", "min_views", "max_views", "min_followers", "max_followers", "limit", "offset"):
                if q.get(k):
                    f[k] = float(q[k]) if k == "max_duration" else int(q[k])
        except ValueError:
            raise err(400, "invalid_filter")
        if q.get("saved") in ("1", "true"):
            f["saved"] = True
        elif q.get("saved") in ("0", "false"):
            f["saved"] = False
        return {"ok": True, **await svc.list_refs(f)}

    async def one(rid: int, reports=True):
        r = await svc.get_ref(rid, reports)
        if not r:
            raise err(404, "not_found")
        return r

    @router.get("/refs/{rid}")
    async def get_ref(rid: int):
        await ready()
        return {"ok": True, "ref": await one(rid)}

    @router.patch("/refs/{rid}")
    async def patch_ref(rid: int, request: Request):
        await ready()
        try:
            hit = await svc.update_ref(rid, await body(request))
        except ValueError as e:
            raise err(400, "invalid_field", str(e))
        if not hit:
            raise err(404, "not_found")
        await audit("creative_ref_updated", request, f"CREATIVE_REF_UPDATED id={rid}")
        return {"ok": True, "ref": await one(rid, False)}

    @router.delete("/refs/{rid}")
    async def delete_ref(rid: int, request: Request):
        await ready()
        if not await svc.delete_ref(rid):
            raise err(404, "not_found")
        await audit("creative_ref_deleted", request, f"CREATIVE_REF_DELETED id={rid}")
        return {"ok": True}

    @router.post("/refs/{rid}/metrics")
    async def add_metrics(rid: int, request: Request):
        await ready()
        d = await body(request)
        try:
            await svc.add_manual_metric(rid, d, _dt(d.get("captured_at")))
        except KeyError:
            raise err(404, "not_found")
        except ValueError as e:
            raise err(400, "invalid_metrics", str(e))
        return {"ok": True, "ref": await one(rid, False)}

    @router.post("/refs/{rid}/refresh")
    async def refresh(rid: int, request: Request):
        await ready()
        r = await one(rid, False)
        if not r.get("url"):
            raise err(400, "no_url", "This reference has no URL to re-read")
        try:
            rec, warnings = await resolve(r["url"])
        except SourceError as e:
            raise src_err(e)
        if rec is None or all(getattr(rec, k) is None for k in ("views", "likes", "comments", "shares", "followers")):
            raise err(409, "no_metrics_source", "No connected source returns metrics for this platform. Add a measurement manually.", warnings=warnings)
        await svc.upsert_reference(rec, snapshot=True)
        return {"ok": True, "warnings": warnings, "ref": await one(rid, False)}

    @router.post("/refs/{rid}/publications")
    async def add_pub(rid: int, request: Request):
        await ready()
        try:
            pid = await svc.add_publication(rid, await body(request))
        except KeyError:
            raise err(404, "not_found")
        except ValueError as e:
            raise err(400, "invalid_field", str(e))
        return {"ok": True, "id": pid, "ref": await one(rid, False)}

    @router.get("/refs/{rid}/export")
    async def export_ref(rid: int, format: str = "md"):
        await ready()
        r = await one(rid)
        if format == "json":
            return Response(__import__("json").dumps(r, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="creative_{rid}.json"'})
        return PlainTextResponse(export.to_markdown(r), headers={"Content-Disposition": f'attachment; filename="creative_{rid}.md"'})

    # ------------------------------------------------------------------ video analysis
    async def start_analysis(aid: int, ref: dict, language: str):
        async def job():
            async with sem:
                await analysis.run_analysis(svc, llm, cfg, aid, ref, envmap, language)
        spawn(job())

    @router.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...), ref_id: int | None = Form(None), title: str = Form(""), niche: str = Form(""), language: str = Form("en")):
        await ready()
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > cfg.max_upload_bytes + 1_000_000:
            raise err(413, "file_too_large", f"File exceeds {cfg.max_upload_mb} MB")
        if not media.ffmpeg_path():
            raise err(503, "ffmpeg_missing", "ffmpeg is not installed on the server")
        if not llm.configured:
            raise err(503, "llm_not_configured", "LLM is not configured")
        async def chunks():
            while True:
                c = await file.read(1 << 20)
                if not c:
                    return
                yield c
        try:
            media.safe_ext(file.filename)
            path, sha, size = await media.save_upload(chunks(), file.filename, cfg)
        except media.MediaError as e:
            raise err(413 if e.code == "file_too_large" else 400, e.code, e.message)
        try:
            if ref_id is not None:
                ref = await svc.get_ref(ref_id, False)
                if not ref:
                    raise err(404, "not_found", "Reference not found")
            else:
                name = re.sub(r"[^\w .-]", "", os.path.splitext(os.path.basename(file.filename or ""))[0])[:120] or "Uploaded video"
                ref_id, _ = await svc.upsert_reference(file_key=f"file:{sha}", platform="other", fields={"title": (title or name)[:500], "niche": niche[:120]}, save=True)
                ref = await svc.get_ref(ref_id, False)
            aid, reused = await svc.create_analysis(ref_id, sha, path)
        except BaseException:
            os.unlink(path)
            raise
        if reused:
            os.unlink(path)                                         # same file already analysed (or being analysed): nothing new to process
        else:
            await start_analysis(aid, ref, language[:8])
        await audit("creative_upload", request, f"CREATIVE_UPLOAD ref={ref_id} analysis={aid} bytes={size} reused={reused}")
        return {"ok": True, "ref_id": ref_id, "analysis_id": aid, "reused": reused, "analysis": await svc.analysis_view(aid)}

    @router.get("/analyses/{aid}")
    async def get_analysis(aid: int):
        await ready()
        v = await svc.analysis_view(aid)
        if not v:
            raise err(404, "not_found")
        return {"ok": True, "analysis": v}

    @router.post("/analyses/{aid}/retry")
    async def retry(aid: int, request: Request):
        await ready()
        a = await svc.get_analysis(aid)
        if not a:
            raise err(404, "not_found")
        if a["status"] != "failed":
            raise err(409, "not_failed", "Only failed analyses can be retried")
        if not a["upload_path"] or not os.path.isfile(a["upload_path"]):
            raise err(409, "upload_missing", "The uploaded file is gone; upload the video again")
        await svc.set_analysis(aid, status="queued", error=None)
        data = {}
        try:
            data = await body(request)
        except HTTPException:
            pass
        await start_analysis(aid, await svc.get_ref(a["ref_id"], False), str(data.get("language") or "en")[:8])
        return {"ok": True, "analysis": await svc.analysis_view(aid)}

    @router.get("/analyses/{aid}/frames/{name}")
    async def frame(aid: int, name: str):
        if not re.fullmatch(r"\d{2}\.jpg", name):
            raise err(404, "not_found")
        p = os.path.join(media.frames_dir(cfg, aid), name)
        if not os.path.isfile(p):
            raise err(404, "not_found")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------------ prompt forge
    @router.post("/forge")
    async def forge_start(request: Request):
        await ready()
        d = await body(request)
        try:
            rid = int(d.get("ref_id"))
            params = forge.normalize_params(d.get("params") or {})
        except (TypeError, ValueError) as e:
            raise err(400, "invalid_params", str(e))
        ref = await svc.get_ref(rid, False)
        if not ref:
            raise err(404, "not_found", "Reference not found")
        a = await svc.get_analysis(int(d["analysis_id"])) if d.get("analysis_id") else await svc.latest_completed_analysis(rid)
        if not a or a["status"] != "completed" or a["ref_id"] != rid:
            raise err(409, "analysis_required", "Run a completed video analysis for this reference first (upload the video in Video Analysis). Prompt Forge does not work from a title or thumbnail alone.")
        if not llm.configured:
            raise err(503, "llm_not_configured", "LLM is not configured")
        pid, reused = await svc.create_prompt(rid, a["id"], params, bool(d.get("regenerate")))
        if not reused:
            full = {"facts": __import__("json").loads(a["facts"] or "null"), "report": __import__("json").loads(a["report"] or "null")}
            async def job():
                async with sem:
                    await forge.run_forge(svc, llm, cfg, pid, ref, full, params)
            spawn(job())
        await audit("creative_forge", request, f"CREATIVE_FORGE ref={rid} prompt={pid} reused={reused}")
        return {"ok": True, "prompt_id": pid, "reused": reused, "prompt": await svc.prompt_view(pid)}

    @router.get("/prompts/{pid}")
    async def get_prompt(pid: int):
        await ready()
        v = await svc.prompt_view(pid)
        if not v:
            raise err(404, "not_found")
        return {"ok": True, "prompt": v}

    # ------------------------------------------------------------------ static UI (public, no data)
    @assets.get("/creative-assets/{name}")
    async def asset(name: str):
        if name not in ("creative.js", "creative.css"):
            raise HTTPException(404)
        return FileResponse(os.path.join(UI_DIR, name), media_type="text/javascript" if name.endswith(".js") else "text/css", headers={"Cache-Control": "no-cache"})

    router.assets = assets                                          # main.py mounts it separately (no auth dependency)
    try:
        media.purge_old(cfg)
    except OSError:
        pass
    return router

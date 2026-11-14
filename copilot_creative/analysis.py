"""Video analysis job: ffmpeg facts (probe, scene cuts, silences) -> sampled frames -> optional transcript -> vision-capable model -> stored report.
The report separates `facts` (measured locally, confirmed) from `report` (model observations tagged observed/inferred/unknown).
Only a handful of downscaled frames are sent to the model (never the video). Nothing is claimed that the evidence does not support:
if the vision model is unavailable the job FAILS with a clear error instead of producing a title/thumbnail-based "analysis"."""
import asyncio
import base64
import logging
import os
import shutil
import tempfile

from copilot_creative import media, safety
from copilot_creative.config import Config
from copilot_creative.llm import LLM, LLMError, extract_json

logger = logging.getLogger("copilot_creative.analysis")
SCHEMA_VERSION = 1
TAGS = ("observed", "inferred", "unknown")

SYSTEM = f"""You are a short-form video analyst for a creative research tool. You receive sampled frames (with timestamps) and measured facts from one video,
and must return ONE JSON object — no prose outside JSON.
{safety.DATA_RULE}
Rules:
- You see only the sampled frames, not the motion between them. Describe what the frames show as "observed"; label anything about motion between frames, intent, audience or cause as "inferred". Use "unknown" when it cannot be established. Never invent lens focal length, FPS, camera coordinates, a person's identity, a music title or an artist.
- Do not identify real people. Describe appearance, clothing, pose, gaze, expression and gestures only. Give an age range only if clearly supported, else "unknown".
- You cannot hear the audio. Audio facts come only from the measured silences and the transcript (if present). For music mood/tempo/sound effects answer "unknown" unless the transcript or on-screen text states them.
- Reasons a format might work are testable HYPOTHESES, not proven causes.
- Write all text values in the requested report language. Keep every value concise (<= 240 chars) except the timeline `action`.
JSON shape (every leaf is a string unless noted; each object also has "basis": "observed"|"inferred"|"unknown"):
{{"summary":{{"topic","format","goal","audience","strength","basis"}},
"timeline":[{{"start":number,"end":number,"shot_size","camera_position","camera_movement","character_motion","action","background","lighting","composition","colors":[string],"on_screen_text","transition","sound_event","function","basis"}}],
"actor":{{"appearance","age_range","clothing","pose","gaze","expression","gestures","movement_sequence","choreography_rhythm","basis"}},
"camera_staging":{{"shot_types","camera_movement","distance_changes","speed","stabilisation","composition","scene_depth","lighting","environment_style","color_grade","basis"}},
"audio":{{"speech","transcript_summary","sound_effects","music_mood_tempo","rhythm_sync","pauses_key_sounds","basis"}},
"script_mechanics":{{"hook","retention","visual_stimulus_changes","development","climax","ending","cta","basis"}},
"why_it_may_work":[{{"hypothesis","evidence"}}],
"limitations":[string]}}
Timeline: one entry per scene or notable change, using the provided scene-cut times where they exist; times in seconds within [0, duration]."""


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def sanitize_report(rep: dict, duration: float) -> dict:
    """Strict shape: unknown keys dropped, strings capped, basis normalised, times clamped. Model output is data, not trusted structure."""
    def s(v, n=240):
        return safety.clean(v, n) if isinstance(v, (str, int, float)) else ""

    def obj(d, keys, n=240):
        d = d if isinstance(d, dict) else {}
        o = {k: s(d.get(k), n) for k in keys}
        o["basis"] = d.get("basis") if d.get("basis") in TAGS else "unknown"
        return o
    tl = []
    for sc in (rep.get("timeline") if isinstance(rep.get("timeline"), list) else [])[:60]:
        if not isinstance(sc, dict):
            continue
        try:
            a, b = float(sc.get("start", 0)), float(sc.get("end", 0))
        except (TypeError, ValueError):
            continue
        a, b = max(0.0, min(a, duration)), max(0.0, min(b, duration))
        e = obj(sc, ("shot_size", "camera_position", "camera_movement", "character_motion", "background", "lighting", "composition", "on_screen_text", "transition", "sound_event", "function"))
        e["action"] = s(sc.get("action"), 600)
        e["colors"] = [s(c, 30) for c in (sc.get("colors") if isinstance(sc.get("colors"), list) else [])[:6]]
        e["start"], e["end"] = round(min(a, b), 2), round(max(a, b), 2)
        tl.append(e)
    tl.sort(key=lambda x: x["start"])
    return {"schema": SCHEMA_VERSION,
            "summary": obj(rep.get("summary"), ("topic", "format", "goal", "audience", "strength")),
            "timeline": tl,
            "actor": obj(rep.get("actor"), ("appearance", "age_range", "clothing", "pose", "gaze", "expression", "gestures", "movement_sequence", "choreography_rhythm")),
            "camera_staging": obj(rep.get("camera_staging"), ("shot_types", "camera_movement", "distance_changes", "speed", "stabilisation", "composition", "scene_depth", "lighting", "environment_style", "color_grade")),
            "audio": obj(rep.get("audio"), ("speech", "transcript_summary", "sound_effects", "music_mood_tempo", "rhythm_sync", "pauses_key_sounds")),
            "script_mechanics": obj(rep.get("script_mechanics"), ("hook", "retention", "visual_stimulus_changes", "development", "climax", "ending", "cta")),
            "why_it_may_work": [{"hypothesis": s(h.get("hypothesis")), "evidence": s(h.get("evidence"))} for h in (rep.get("why_it_may_work") if isinstance(rep.get("why_it_may_work"), list) else [])[:8] if isinstance(h, dict)],
            "limitations": [s(x) for x in (rep.get("limitations") if isinstance(rep.get("limitations"), list) else [])[:10]]}


async def transcribe(audio_path: str, cfg: Config, env) -> dict:
    """Optional speech-to-text through an OpenAI-compatible /audio/transcriptions provider configured by env. Never raises: returns a status dict."""
    if not (cfg.stt_url and env.get("COPILOT_CREATIVE_STT_KEY")):
        return {"available": False, "reason": "No speech-to-text provider configured (COPILOT_CREATIVE_STT_URL / COPILOT_CREATIVE_STT_KEY / COPILOT_CREATIVE_STT_MODEL)."}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            with open(audio_path, "rb") as f:
                r = await c.post(cfg.stt_url, headers={"Authorization": f"Bearer {env['COPILOT_CREATIVE_STT_KEY']}"},
                                 data={"model": cfg.stt_model or "whisper-1", "response_format": "json"}, files={"file": ("audio.mp3", f, "audio/mpeg")})
        if r.status_code >= 400:
            return {"available": False, "reason": f"Speech-to-text provider returned HTTP {r.status_code}."}
        text = safety.clean(r.json().get("text", ""), cfg.transcript_chars)
        return {"available": True, "text": text, "chars": len(text)} if text else {"available": True, "text": "", "chars": 0, "note": "No speech detected."}
    except Exception as e:
        return {"available": False, "reason": f"Speech-to-text failed ({type(e).__name__})."}


async def run_analysis(svc, llm: LLM, cfg: Config, aid: int, ref: dict, env, language: str = "en") -> None:
    """Executes one queued analysis row. Always ends in `completed` or `failed`; the uploaded file is deleted on success."""
    tmp = tempfile.mkdtemp(prefix="cr_", dir=media.uploads_dir(cfg))
    log = lambda m: svc.log_analysis(aid, m)           # noqa: E731
    try:
        a = await svc.get_analysis(aid)
        path = a["upload_path"]
        if not path or not os.path.isfile(path):
            raise media.MediaError("upload_missing", "The uploaded video is no longer on the server (cleaned up). Upload the file again.")
        await svc.set_analysis(aid, status="processing", error=None)
        await log("Checking vision model")
        await llm.require_vision(cfg.vision_model)
        await log("Reading video metadata")
        pr = await media.probe(path)
        if pr.duration_s > cfg.max_duration_s:
            raise media.MediaError("too_long", f"Video is {pr.duration_s:.0f}s; the limit is {cfg.max_duration_s}s (COPILOT_CREATIVE_MAX_DURATION_S)")
        await log("Detecting scene cuts and silences")
        cuts, sil = await asyncio.gather(media.scene_cuts(path), media.silences(path) if pr.has_audio else asyncio.sleep(0, result=[]))
        stamps = media.pick_timestamps(pr.duration_s, cuts, cfg.max_frames)
        await log(f"Extracting {len(stamps)} frames")
        fdir = media.frames_dir(cfg, aid)
        for old in os.listdir(fdir):
            os.unlink(os.path.join(fdir, old))
        frames = []
        for i, t in enumerate(stamps, 1):
            out = os.path.join(fdir, f"{i:02d}.jpg")
            await media.extract_frame(path, t, out, cfg.frame_width)
            frames.append({"n": i, "t": t, "file": f"{i:02d}.jpg"})
        transcript = {"available": False, "reason": "The video has no audio track."}
        if pr.has_audio:
            if cfg.stt_url:
                await log("Transcribing audio")
                ap = os.path.join(tmp, "a.mp3")
                await media.extract_audio(path, ap)
                transcript = await transcribe(ap, cfg, env)
            else:
                transcript = await transcribe("", cfg, env)
        facts = {"probe": pr.as_dict(), "scene_cuts_s": cuts, "cut_count": len(cuts), "avg_shot_length_s": round(pr.duration_s / (len(cuts) + 1), 2),
                 "silences": sil, "frames_sampled": len(frames), "frame_times_s": stamps, "transcript": transcript,
                 "audio_listened": False, "visual_coverage": f"{len(frames)} sampled frames (motion between frames is not observed)"}
        await svc.set_analysis(aid, frames=frames, facts=facts)

        meta = (f"Platform: {ref.get('platform')}\n" f"Title/caption: {safety.fence('title', ref.get('title'), 500)}\n" f"Author: {safety.fence('author', ref.get('author'), 120)}\n"
                f"User notes: {safety.fence('notes', ref.get('notes'), 800)}")
        content = [{"type": "text", "text": (
            f"Report language: {language}\nMeasured facts (confirmed): duration {pr.duration_s:.1f}s, {pr.width}x{pr.height}, {pr.fps} fps, audio track: {pr.has_audio}.\n"
            f"Scene cuts at (s): {cuts[:80]}\nSilences (>=0.4 s): {sil[:40]}\n"
            f"Transcript: {safety.fence('transcript', transcript.get('text'), cfg.transcript_chars) if transcript.get('text') else 'NOT AVAILABLE — ' + str(transcript.get('reason') or transcript.get('note') or '')}\n"
            f"Reference metadata:\n{meta}\nSampled frames follow, each preceded by its timestamp.")}]
        for f in frames:
            content.append({"type": "text", "text": f"Frame {f['n']} at t={f['t']:.2f}s"})
            content.append({"type": "image_url", "image_url": {"url": _b64(os.path.join(fdir, f['file']))}})
        await log(f"Calling {cfg.vision_model} with {len(frames)} frames")
        text, usage = await llm.chat(cfg.vision_model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}], max_tokens=7000, temperature=0.2)
        try:
            raw = extract_json(text)
        except LLMError:
            await log("Model reply was not valid JSON; asking once more")
            text, u2 = await llm.chat(cfg.vision_model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}], max_tokens=9000, temperature=0.1)
            raw, usage = extract_json(text), {k: (usage.get(k) or 0) + (u2.get(k) or 0) for k in u2}
        rep = sanitize_report(raw, pr.duration_s)
        rep["injection_flags"] = safety.flags(ref.get("title"), ref.get("notes"), transcript.get("text"))
        rep["limitations"] = [f"Visual analysis is based on {len(frames)} sampled frames; motion between frames, audio and music were not directly observed."] + rep["limitations"]
        await svc.set_analysis(aid, status="completed", error=None, report=rep, model=cfg.vision_model, usage=usage, upload_path=None)
        try:
            os.unlink(path)
        except OSError:
            pass
        await log("Completed")
    except (media.MediaError, LLMError) as e:
        logger.warning("analysis %s failed: %s", aid, e.code)
        await svc.set_analysis(aid, status="failed", error=f"{e.code}: {e.message}"[:500])
        await log(f"Failed: {e.code}")
    except Exception as e:
        logger.exception("analysis %s crashed", aid)
        await svc.set_analysis(aid, status="failed", error=f"internal_error: {type(e).__name__}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

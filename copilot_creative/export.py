"""Markdown / JSON export of one library entry (reference + measurements + analysis + prompts + own publications)."""
import json


def _kv(d: dict, skip=("basis",)) -> list[str]:
    return [f"- **{k.replace('_', ' ')}**: {v}" for k, v in (d or {}).items() if k not in skip and v not in (None, "", [])]


def to_markdown(ref: dict) -> str:
    L = [f"# {ref.get('title') or 'Untitled reference'}", "",
         f"- Platform: {ref['platform']} · Source: {ref['source']} · Discovered: {ref['discovered_at']}",
         f"- URL: {ref.get('url') or '—'}", f"- Author: {ref.get('author') or '—'} · Niche: {ref.get('niche') or '—'} · GEO: {ref.get('geo') or '—'} · Language: {ref.get('language') or '—'}",
         f"- Trend status: {ref['trend_status']} · Production: {ref['production_status']} · Tags: {', '.join(ref['tags']) or '—'}", ""]
    m = ref["metrics"]
    L += ["## Metrics (latest; empty = not provided by the source)", "", "| views | likes | comments | shares | followers | updated |", "|---|---|---|---|---|---|",
          "| " + " | ".join(str(m[k]) if m[k] is not None else "" for k in ("views", "likes", "comments", "shares", "followers")) + f" | {ref.get('metrics_updated_at') or ''} |", ""]
    a = ref["anomaly"]
    if a.get("score") is not None:
        L += [f"**Anomaly score {a['score']}/100** — {a['disclaimer']}", ""] + [f"- {k}: {c['basis']} (weight {c['weight']})" for k, c in a["components"].items()] + [""]
    if ref.get("notes"):
        L += ["## Notes", "", ref["notes"], ""]
    for an in ref.get("analyses", []):
        if an["status"] != "completed" or not an.get("report"):
            continue
        r, f = an["report"], an.get("facts") or {}
        L += [f"## Video analysis #{an['id']} ({an['model']})", "", "### Measured facts (confirmed)", "", f"```json\n{json.dumps({k: f.get(k) for k in ('probe', 'cut_count', 'avg_shot_length_s', 'silences')}, ensure_ascii=False)}\n```", ""]
        for title, key in (("Summary", "summary"), ("Actor", "actor"), ("Camera & staging", "camera_staging"), ("Audio", "audio"), ("Script mechanics", "script_mechanics")):
            L += [f"### {title} _(basis: {r[key].get('basis')})_", ""] + _kv(r[key]) + [""]
        L += ["### Timeline", ""]
        for s in r["timeline"]:
            L += [f"**{s['start']}s – {s['end']}s** ({s['basis']}) — {s['action']}"] + _kv({k: v for k, v in s.items() if k not in ("start", "end", "action", "basis")}) + [""]
        L += ["### Why it may work (hypotheses)", ""] + [f"- {h['hypothesis']} — _{h['evidence']}_" for h in r["why_it_may_work"]] + ["", "### Limitations", ""] + [f"- {x}" for x in r["limitations"]] + [""]
    for p in ref.get("prompts", []):
        if p["status"] != "completed" or not p.get("result"):
            continue
        x = p["result"]
        L += [f"## Prompt Forge #{p['id']} — {x['preset']['label']}", "", f"Params: `{json.dumps(p['params'], ensure_ascii=False)}`", "", "### Creative blueprint", ""] + _kv(x["blueprint"]) + ["", "### Master prompt", "", "```", x["master_prompt"], "```", "", "### Storyboard", ""]
        for s in x["storyboard"]:
            L += [f"{s['n']}. **{s['start']}–{s['end']}s** {s['action']} · camera: {s['camera_motion']} · light: {s['lighting']} · text: {s['on_screen_text']} · sound: {s['sound']}"]
        L += ["", "### Shot prompts", ""] + [f"{s['n']}. ({s['duration']}s) {s['prompt']}\n   - continuity: {s['continuity']}" for s in x["shot_prompts"]]
        for title, key in (("Character", "character"), ("Environment", "environment"), ("Camera & motion", "camera_motion"), ("Sound design", "sound_design")):
            if x.get(key):
                L += ["", f"### {title}", ""] + _kv(x[key])
        L += ["", "### Negative prompt", "", x["negative_prompt"], "", "### Adaptations", ""] + [f"- **{a['name']}** ({a['kind']}): {a['summary']} — hook: {a['hook']}" for a in x["adaptations"]]
        L += ["", "### Production checklist", ""] + [f"- [ ] {c}" for c in x["checklist"]]
        L += ["", "### Provenance", "", "Confirmed from source:"] + [f"- {c}" for c in x["provenance"]["confirmed_from_source"]] + ["", "AI-proposed:"] + [f"- {c}" for c in x["provenance"]["ai_proposed"]]
        L += [""] + [f"> ⚠ {w}" for w in x["warnings"]] + [""]
    if ref.get("publications"):
        L += ["## Own publications", ""] + [f"- {p['posted_at'] or ''} {p['url'] or ''} views={p['views']} likes={p['likes']} comments={p['comments']} shares={p['shares']} {p['notes'] or ''}" for p in ref["publications"]]
    return "\n".join(L).strip() + "\n"

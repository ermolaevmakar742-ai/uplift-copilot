"""Learner v1: critiques -> examples -> patterns -> candidate improvements -> offline evaluation. Offline / shadow, idempotent.

Everything is DERIVED from stored critiques + the existing shadow rows' reward (Reward Resolver v1, incl. append-only delayed
corrections; no second reward). Patterns come from what was observed (stage x reply class x negative critic reasons x response
form), never from a predefined list. The critic is not ground truth: example labels come only from the reward; a missing reward
leaves the example 'unevaluated'. Nothing here applies anything: lifecycle stops at OFFLINE_EVALUATED, there is no apply path."""
import math
from datetime import datetime, timezone

from sqlalchemy import select, update

from copilot_learning import contract as C
from copilot_learning.store import critiques, examples, ensure_tables, improvements, patterns, _insert
from copilot_shadow.store import shadow_decisions

MIN_OBSERVED = 3             # samples before a pattern with negative reasons becomes an OBSERVED improvement
MIN_PATTERN_SAMPLES = 20     # labelled samples before it may become a CANDIDATE
MIN_GROUP = 10               # per-group samples for a "sufficient" offline comparison
CONTRA = {"TOO_PUSHY": ["never raise pressure to compensate for silence"], "TOO_EARLY_OFFER": ["do not delay an offer the client explicitly asks for"],
          "IGNORED_OBJECTION": ["acknowledgement must not turn into a promise or guarantee"], "NO_QUESTION": ["not after a client refusal or stop request"],
          "TOO_LONG": ["do not drop information the client asked for"], "REPETITIVE": ["do not vary wording of safety/legal statements"],
          "WRONG_STAGE": ["post-deposit support questions are legitimate"]}
PROBLEMS = {  # reason -> (current behaviour, candidate change); effect is always "unknown until offline evaluation"
    "TOO_EARLY_OFFER": ("offer / call-to-action sent at an early stage", "ask one clarifying question before any offer"),
    "WRONG_STAGE": ("action request that does not match the funnel stage", "match the next step to the current stage"),
    "IGNORED_OBJECTION": ("call-to-action sent without acknowledging the stated objection", "acknowledge the objection, then ask about it"),
    "NO_QUESTION": ("reply without a question", "close the reply with one targeted question"),
    "REPETITIVE": ("reply nearly identical to a recent one", "vary the wording / add new information"),
    "TOO_LONG": ("reply longer than a chat message", "shorten to the essential point"),
    "TOO_PUSHY": ("pressure wording or several calls-to-action", "remove urgency wording, keep a single soft next step")}


class LifecycleError(ValueError):
    pass


def _aware(ts):
    return ts if isinstance(ts, datetime) and ts.tzinfo else (ts.replace(tzinfo=timezone.utc) if isinstance(ts, datetime) else None)


def _neg(reasons):
    return sorted(r for r in reasons if r in C.NEGATIVE_REASONS)[:3]


# ---- pattern signatures: versioned, registered; a new version = a new function, old ids stay comparable ----
def sig_v1(stage, reply_class, reasons, features) -> dict:
    f = features or {}
    return {"stage": stage, "reply_class": reply_class, "neg": _neg(reasons),
            "question": bool(f.get("has_question")), "offer": bool(f.get("cta"))}


SIGNATURES = {"pattern-1": sig_v1}


def signature_text(sig: dict) -> str:
    return f"stage={sig['stage']}|reply={sig['reply_class']}|neg={','.join(sig['neg']) or 'NONE'}|q={int(sig['question'])}|offer={int(sig['offer'])}"


def pattern_id(sig: dict, version: str) -> str:
    return "pt_" + C.digest(sig, version)


def quality_of(reward) -> str:
    return "unevaluated" if reward is None else "success" if reward > 0 else "failure" if reward < 0 else "neutral"


def effective_reward(row) -> tuple:
    """(reward, basis, reward_version, params_id) from a shadow row; the LAST delayed correction wins, the original is never edited."""
    corr = row.get("reward_corrections") or []
    detail = row.get("reward_detail") or {}
    if corr and corr[-1].get("corrected_reward") is not None:
        return corr[-1]["corrected_reward"], "corrected", corr[-1].get("reward_version"), corr[-1].get("params_id")
    return row.get("reward"), "original", row.get("reward_version"), detail.get("params_id")


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


async def sync_examples(sf, pattern_version: str = C.PATTERN_VERSION) -> int:
    """One example per critique (idempotent). Unusable (low-confidence) critiques become 'unreliable', outside patterns."""
    await ensure_tables(sf)
    sig_fn = SIGNATURES[pattern_version]
    async with sf() as s:
        have = {r[0] for r in (await s.execute(select(examples.c.decision_id).where(examples.c.example_version == C.EXAMPLE_VERSION))).all()}
        rows = (await s.execute(select(critiques))).mappings().all()
    new = 0
    async with sf() as s:
        for c in rows:
            if c["decision_id"] in have:
                continue
            usable = bool(c["usable"])
            sig = sig_fn(c["stage"], c["reply_class"], c["reasons"], c["response_features"])
            await s.execute(_insert(s, examples).values(
                example_id="ex_" + C.digest(c["decision_id"], C.EXAMPLE_VERSION, c["critic_version"]), decision_id=c["decision_id"],
                example_version=C.EXAMPLE_VERSION, critic_version=c["critic_version"],
                state_signature=f"{c['stage']}|{c['reply_class']}", stage=c["stage"], reply_class=c["reply_class"],
                reasons=c["reasons"], critic_overall=(c["scores"] or {}).get("overall_quality"), usable=usable,
                pattern_id=pattern_id(sig, pattern_version) if usable else None, pattern_version=pattern_version if usable else None,
                quality="unevaluated" if usable else "unreliable", history=[]).on_conflict_do_nothing())
            new += 1
        await s.commit()
    return new


async def evaluate_examples(sf) -> int:
    """Attach the CURRENT effective reward of each decision to its example (label + audit history). Idempotent: unchanged = no write."""
    await ensure_tables(sf)
    async with sf() as s:
        exs = (await s.execute(select(examples).where(examples.c.usable.is_(True)))).mappings().all()
        ids = [e["decision_id"] for e in exs]
        sd = {}
        if ids:
            try:
                sd = {r["decision_id"]: dict(r) for r in (await s.execute(select(
                    shadow_decisions.c.decision_id, shadow_decisions.c.reward, shadow_decisions.c.reward_version,
                    shadow_decisions.c.reward_detail, shadow_decisions.c.reward_corrections).where(
                        shadow_decisions.c.decision_id.in_(ids)))).mappings().all()}
            except Exception:                                         # shadow store unreadable: label nothing
                sd = {}
    changed = 0
    async with sf() as s:
        for e in exs:
            row = sd.get(e["decision_id"])
            if row is None or row["reward_version"] is None:
                continue                                              # missing outcome/reward: stays 'unevaluated', nothing invented
            reward, basis, rv, pid = effective_reward(row)
            q = quality_of(reward)
            hist = list(e["history"] or [])
            entry = {"reward": reward, "basis": basis, "reward_version": rv, "params_id": pid, "quality": q}
            if hist and hist[-1] == entry:
                continue
            hist.append(entry)
            await s.execute(update(examples).where(examples.c.example_id == e["example_id"]).values(
                quality=q, reward=reward, reward_version=rv, reward_basis=basis, history=hist, evaluated_at=datetime.now(timezone.utc)))
            changed += 1
        await s.commit()
    return changed


async def mine_patterns(sf, pattern_version: str = C.PATTERN_VERSION) -> int:
    """Recompute every pattern of `pattern_version` from the usable examples (deterministic, idempotent upsert)."""
    await ensure_tables(sf)
    sig_fn = SIGNATURES[pattern_version]
    async with sf() as s:
        exs = (await s.execute(select(examples).where(examples.c.usable.is_(True)))).mappings().all()
        crit = {r["decision_id"]: r for r in (await s.execute(select(critiques))).mappings().all()}
    agg: dict = {}
    for e in exs:
        c = crit.get(e["decision_id"])
        if c is None:
            continue
        sig = sig_fn(c["stage"], c["reply_class"], c["reasons"], c["response_features"])
        a = agg.setdefault(pattern_id(sig, pattern_version), {"sig": sig, "n": 0, "success": 0, "neutral": 0, "failure": 0, "unevaluated": 0, "ts": []})
        a["n"] += 1
        a[e["quality"] if e["quality"] in ("success", "neutral", "failure") else "unevaluated"] += 1
        a["ts"].append(_aware(c["created_at"]))
    async with sf() as s:
        for pid, a in sorted(agg.items()):
            ts = [t for t in a["ts"] if t]
            vals = dict(pattern_version=pattern_version, signature=a["sig"], signature_text=signature_text(a["sig"]), sample_count=a["n"],
                        success=a["success"], neutral=a["neutral"], failure=a["failure"], unevaluated=a["unevaluated"],
                        confidence=round(min(1.0, (a["n"] - a["unevaluated"]) / MIN_PATTERN_SAMPLES), 2),
                        first_seen=min(ts) if ts else None, last_seen=max(ts) if ts else None)
            if (await s.execute(select(patterns.c.pattern_id).where(patterns.c.pattern_id == pid))).first():
                await s.execute(update(patterns).where(patterns.c.pattern_id == pid).values(**vals))
            else:
                await s.execute(patterns.insert().values(pattern_id=pid, **vals))
        await s.commit()
    return len(agg)


async def propose_improvements(sf, pattern_version: str = C.PATTERN_VERSION) -> int:
    """Patterns carrying negative critic reasons -> OBSERVED / CANDIDATE improvements (never applied)."""
    await ensure_tables(sf)
    async with sf() as s:
        pats = (await s.execute(select(patterns).where(patterns.c.pattern_version == pattern_version))).mappings().all()
        exs = (await s.execute(select(examples).where(examples.c.usable.is_(True)))).mappings().all()
        have = {r["improvement_id"]: r for r in (await s.execute(select(improvements))).mappings().all()}
    created = 0
    async with sf() as s:
        for p in pats:
            neg = p["signature"].get("neg") or []
            if not neg or p["sample_count"] < MIN_OBSERVED:
                continue
            labelled = p["sample_count"] - p["unevaluated"]
            main = neg[0]
            iid = "imp_" + C.digest(p["pattern_id"], C.IMPROVEMENT_VERSION, C.LEARNER_VERSION)
            target = "CANDIDATE" if labelled >= MIN_PATTERN_SAMPLES else "OBSERVED"
            support = sorted(e["example_id"] for e in exs if e["pattern_id"] == p["pattern_id"])[:5]
            contra = sorted({x for r in neg for x in CONTRA.get(r, ())}) + (["LOW_LABEL_COVERAGE"] if labelled < MIN_PATTERN_SAMPLES else [])
            cur = have.get(iid)
            if cur is None:
                await s.execute(improvements.insert().values(
                    improvement_id=iid, improvement_version=C.IMPROVEMENT_VERSION, learner_version=C.LEARNER_VERSION, pattern_id=p["pattern_id"],
                    problem=main, observed_pattern=p["signature"], evidence_count=labelled, current_behavior=PROBLEMS[main][0],
                    candidate_change=PROBLEMS[main][1], expected_effect="unknown until offline evaluation (observational, not causal)",
                    confidence=p["confidence"], supporting_examples=support, contraindications=contra, state=target,
                    history=[{"state": target, "at": datetime.now(timezone.utc).isoformat(), "evidence": labelled}]))
                created += 1
            else:
                vals = dict(evidence_count=labelled, confidence=p["confidence"], supporting_examples=support, contraindications=contra,
                            updated_at=datetime.now(timezone.utc))
                if cur["state"] == "OBSERVED" and target == "CANDIDATE":     # promotion only; never demoted, never past OFFLINE_EVALUATED
                    vals["state"] = "CANDIDATE"
                    vals["history"] = list(cur["history"]) + [{"state": "CANDIDATE", "at": vals["updated_at"].isoformat(), "evidence": labelled}]
                await s.execute(update(improvements).where(improvements.c.improvement_id == iid).values(**vals))
        await s.commit()
    return created


async def offline_evaluate(sf, improvement_id: str) -> dict:
    """OBSERVATIONAL comparison on the stored examples of the same (stage, reply class) cell: those WITH the pattern's negative
    reasons vs those WITHOUT. Reports counts and Wilson intervals; claims no causality. Moves CANDIDATE -> OFFLINE_EVALUATED."""
    await ensure_tables(sf)
    async with sf() as s:
        imp = (await s.execute(select(improvements).where(improvements.c.improvement_id == improvement_id))).mappings().first()
        if imp is None:
            raise LifecycleError("unknown improvement")
        if imp["state"] != "CANDIDATE":
            raise LifecycleError("offline evaluation requires state CANDIDATE, got " + imp["state"])
        sig = imp["observed_pattern"]
        exs = (await s.execute(select(examples).where(examples.c.usable.is_(True), examples.c.stage == sig["stage"],
                                                      examples.c.reply_class == sig["reply_class"]))).mappings().all()
    lab = [e for e in exs if e["quality"] in ("success", "neutral", "failure")]
    A = [e for e in lab if set(sig["neg"]) <= set(e["reasons"])]
    B = [e for e in lab if not set(sig["neg"]) & set(e["reasons"])]
    g = lambda grp: {"n": len(grp), "success": sum(e["quality"] == "success" for e in grp),
                     "rate": round(sum(e["quality"] == "success" for e in grp) / len(grp), 4) if grp else None,
                     "wilson95": wilson(sum(e["quality"] == "success" for e in grp), len(grp))}
    ev = {"version": C.OFFLINE_EVAL_VERSION, "with_reasons": g(A), "without_reasons": g(B),
          "sufficient": len(A) >= MIN_GROUP and len(B) >= MIN_GROUP, "causal": False, "kind": "observational",
          "success_rate_diff": (round(g(B)["rate"] - g(A)["rate"], 4) if A and B else None)}
    now = datetime.now(timezone.utc)
    async with sf() as s:
        await s.execute(update(improvements).where(improvements.c.improvement_id == improvement_id, improvements.c.state == "CANDIDATE").values(
            state="OFFLINE_EVALUATED", offline_eval=ev, updated_at=now,
            history=list(imp["history"]) + [{"state": "OFFLINE_EVALUATED", "at": now.isoformat(), "sufficient": ev["sufficient"]}]))
        await s.commit()
    return ev


async def advance(sf, improvement_id: str, to_state: str) -> bool:
    """Only the automated v1 transitions exist. HUMAN_REVIEW / APPROVED / REJECTED are NOT reachable from code."""
    if to_state not in C.LIFECYCLE:
        raise LifecycleError("unknown state")
    if to_state not in C.REACHABLE_V1:
        raise LifecycleError(f"{to_state} is outside Learning Loop v1 (human decision, not automated)")
    raise LifecycleError("states advance only through propose_improvements / offline_evaluate")


async def retrieve(sf, stage, reply_class, reasons=(), k: int = 3) -> list:
    """Research-side deterministic similarity: successful, usable examples ranked by signature overlap. References only."""
    await ensure_tables(sf)
    async with sf() as s:
        exs = (await s.execute(select(examples).where(examples.c.quality == "success", examples.c.usable.is_(True)))).mappings().all()
    scored = []
    for e in exs:
        sc = 2 * (e["stage"] == stage) + 2 * (e["reply_class"] == reply_class) + len(set(reasons) & set(e["reasons"]))
        if sc > 0:
            scored.append((-sc, -(e["critic_overall"] or 0), e["example_id"], e))
    scored.sort(key=lambda t: t[:3])
    return [{"example_id": e["example_id"], "decision_id": e["decision_id"], "pattern_id": e["pattern_id"], "score": -sc,
             "critic_overall": e["critic_overall"], "shadow_only": True} for sc, _, _, e in scored[:k]]


async def calibration(sf) -> list:
    """Critic score vs actual reward label (the critic is NOT ground truth): per overall-quality bucket."""
    await ensure_tables(sf)
    async with sf() as s:
        exs = (await s.execute(select(examples).where(examples.c.usable.is_(True)))).mappings().all()
    out = []
    for lo, hi in ((0, 39), (40, 59), (60, 79), (80, 100)):
        grp = [e for e in exs if e["critic_overall"] is not None and lo <= e["critic_overall"] <= hi]
        lab = [e for e in grp if e["quality"] in ("success", "neutral", "failure")]
        out.append({"bucket": f"{lo}-{hi}", "n": len(grp), "labelled": len(lab),
                    "mean_reward": round(sum(e["reward"] for e in lab) / len(lab), 4) if lab else None,
                    "success_rate": round(sum(e["quality"] == "success" for e in lab) / len(lab), 4) if lab else None})
    return out


async def run_once(sf) -> dict:
    """One idempotent learning pass: examples -> labels -> patterns -> improvements. Applies nothing."""
    ex = await sync_examples(sf)
    ev = await evaluate_examples(sf)
    pt = await mine_patterns(sf)
    im = await propose_improvements(sf)
    return {"new_examples": ex, "labels_updated": ev, "patterns": pt, "new_improvements": im, "learner_version": C.LEARNER_VERSION}

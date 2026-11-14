"""Language policy block appended to the system prompt (the localization itself happens in the existing LLM call)."""
import re
from typing import Any, List, Mapping, Optional, Sequence

from .models import GeoProfile, GeoResolution

MODE_HINTS = {
    "casual": "relaxed, curious and friendly",
    "hard": "calm and empathetic; acknowledge the concern in a few natural words before answering; never defensive",
    "agent": "direct and concrete; at most one clear call to action",
    "retention": "warm and appreciative; no pressure",
}
_EARLY = {"new", "visited", "conversation"}
_LATE = {"deposit", "deposit_attempt", "deposited", "converted", "reactivated", "withdraw_request", "withdrawn"}


def _stage_hint(stage: Optional[str]) -> str:
    s = (stage or "new").strip().lower()
    if s in _EARLY:
        return "early conversation: sound like getting to know the person, do not push"
    if s in _LATE:
        return "client already acts on the offer: sound supportive and practical"
    return "mid-funnel: sound helpful and clear, keep it light"


def client_style(history: Optional[Sequence[Mapping[str, Any]]], n: int = 5) -> List[str]:
    """Derived style features only. Client text itself is never copied into the prompt."""
    msgs = [m.get("content") for m in reversed(history or []) if (m or {}).get("role") == "user" and isinstance(m.get("content"), str)]
    msgs = msgs[:n]
    if not msgs:
        return []
    words = [len(m.split()) for m in msgs]
    avg = sum(words) / len(words)
    out = []
    if avg <= 6:
        out.append("The client writes very short messages: keep yours equally short.")
    elif avg >= 25:
        out.append("The client writes long messages: you may be slightly fuller, still conversational.")
    if any(re.search(r"\b(usted|ustedes|señor|señora|senhor|senhora)\b", m, re.I) for m in msgs):
        out.append("The client uses formal address: mirror it politely.")
    if sum(1 for m in msgs if m == m.lower()) >= max(2, len(msgs) // 2 + 1):
        out.append("The client writes casually in lowercase: do not sound formal or polished.")
    return out


def build_policy_block(
    profile: GeoProfile,
    resolution: GeoResolution,
    stage: Optional[str] = None,
    regime: Optional[str] = None,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    """Empty string when the base persona already is this locale (es-MX): zero change."""
    if not profile.prompt_override:
        return ""
    L: List[str] = []
    L.append("\n\n## LANGUAGE POLICY (applies to THIS response; highest priority for language and register)")
    L.append("Target locale: %s (%s). Resolved from: %s, confidence %.2f." % (profile.code, profile.label, resolution.source, resolution.confidence))
    if profile.language == "pt":
        L.append("Write this response in Brazilian Portuguese, even though most instructions above are in Spanish/Russian. "
                 "Spanish sample phrases quoted above (e.g. «Te cuento mas?») only show the INTENT: express the same intent naturally in Portuguese.")
    else:
        L.append("This target locale OVERRIDES the generic instruction «Responde SIEMPRE en español mexicano» and every "
                 "Mexican-specific word, example or phrase elsewhere in this prompt (e.g. órale, no manches, chavo, platicar, CDMX) "
                 "for this response. Express the same intent in natural %s." % profile.label)
    L.append("Every other rule above (length, emoji, punctuation, persona, goals, sales logic) stays unchanged.")
    L.append("You may change only wording, register and rhythm. You must NOT change meaning, offer, prices/amounts/currency, links, "
             "@usernames, promo codes, the call-to-action intent, the sales decision, or any fact about the client.")
    if profile.address_rule:
        L.append("Address: " + profile.address_rule)
    if profile.vocabulary:
        L.append("Preferred vocabulary: " + "; ".join("%s → %s" % kv for kv in profile.vocabulary.items()) + ". Use only where it fits naturally.")
    if profile.patterns:
        L.append("Natural conversational patterns: " + " | ".join(profile.patterns))
    bad = [r.reason for r in profile.forbidden]
    if bad:
        L.append("Avoid (sounds unnatural here): " + "; ".join(dict.fromkeys(bad)) + ".")
    if profile.cta:
        opts = "; ".join("%s: %s" % (k, " / ".join(v)) for k, v in profile.cta.items())
        L.append("If this response already contains a call to action, word it locally (vary it, never repeat the previous wording): " + opts + ".")
    tone = list(profile.tone_rules)
    if regime and regime.lower() in MODE_HINTS:
        tone.append("Mode style (%s): %s." % (regime.lower(), MODE_HINTS[regime.lower()]))
    tone.append("Stage style: " + _stage_hint(stage) + ". (Style only: do not change what the response decides or offers.)")
    L.append("Tone: " + " ".join(tone))
    cs = client_style(history)
    if cs:
        L.append("Client register: " + " ".join(cs))
    if profile.examples:
        L.append("Style references (do not copy verbatim): " + " | ".join(profile.examples))
    L.append("Write one short natural message as a real person would. Do not translate, do not sound like a customer-service script, "
             "do not reuse the structure of your previous messages.")
    return "\n".join(L) + "\n"

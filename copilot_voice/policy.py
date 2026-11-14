"""Voice policy v1: deterministic, cheap, no LLM. Replaceable via the VoicePolicy protocol (ML/LLM later)."""
import re
from typing import Protocol

from .models import (DEFAULT_VOICE_PROFILE, POLICY_VERSION, VoiceContext, VoicePriority, VoiceReason)

THRESHOLD = 0.6
COOLDOWN_SECONDS = 6 * 3600
MIN_CHARS = 60            # shorter replies are never voiced (unless the client explicitly asked)
LONG_CHARS = 350
MAX_CHARS = 1200          # beyond this a voice note is impractical -> NO

_ASK_VOICE = re.compile(
    r"(голосов|голосом|аудио ?сообщ|скажи голосом|запиши (?:мне )?(?:голос|войс)|войс|"
    r"nota de voz|mensaje de voz|m[aá]ndame un audio|un audio|"
    r"voice (?:message|note)|send (?:me )?(?:a )?voice)", re.I)
_EMOTION = re.compile(r"(понима|пережива|поддерж|не переживай|comprendo|entiendo|te apoyo|no te preocupes|"
                      r"i understand|don't worry|💙|🤍)", re.I)
_PERSUADE = re.compile(r"(вот почему|поверь|гаранти|смотри,|давай я объясню|por eso|conf[ií]a|te lo explico|"
                       r"let me explain|trust me)", re.I)
_SERVICE_REGIMES = {"analyst", "system", "service"}


class VoicePolicy(Protocol):
    version: str

    def evaluate(self, ctx: VoiceContext) -> "PolicyOutcome": ...


class PolicyOutcome:
    __slots__ = ("score", "reason", "priority", "should_send")

    def __init__(self, score, reason, priority, should_send):
        self.score, self.reason, self.priority, self.should_send = score, reason, priority, should_send


def _no(reason=VoiceReason.OTHER, score=0.0):
    return PolicyOutcome(score, reason, VoicePriority.LOW, False)


class RulePolicyV1:
    version = POLICY_VERSION
    voice_profile = DEFAULT_VOICE_PROFILE

    def evaluate(self, ctx: VoiceContext) -> PolicyOutcome:
        text = (ctx.reply_text or "").strip()
        n = len(text)
        if not n or ctx.is_service or (ctx.regime or "").lower() in _SERVICE_REGIMES:
            return _no()
        cd = ctx.seconds_since_last_voice
        in_cooldown = cd is not None and cd < COOLDOWN_SECONDS
        asked = bool(_ASK_VOICE.search(ctx.inbound_text or ""))

        if asked:
            # explicit request wins over length/cooldown-lite: only an absurd text size blocks it
            if n > MAX_CHARS * 2:
                return _no()
            return PolicyOutcome(0.95, VoiceReason.CLIENT_REQUESTED_VOICE, VoicePriority.HIGH, True)
        if in_cooldown or n < MIN_CHARS or n > MAX_CHARS:
            return _no()

        signals = []   # (weight, reason): strongest sets the reason, each extra signal adds 0.1
        if n >= LONG_CHARS:
            signals.append((0.55, VoiceReason.LONG_TEXT))
        if (ctx.regime or "").lower() == "hard" or (ctx.stage or "").lower() in {"objection", "hesitation"}:
            signals.append((0.55, VoiceReason.OBJECTION_HANDLING))
        if _EMOTION.search(text):
            signals.append((0.5, VoiceReason.EMOTIONAL_EXPLANATION))
        if _PERSUADE.search(text):
            signals.append((0.5, VoiceReason.PERSUASION))
        if not signals:
            return _no()
        signals.sort(key=lambda s: -s[0])
        score = round(min(signals[0][0] + 0.1 * (len(signals) - 1), 0.9), 2)
        ok = score >= THRESHOLD
        return PolicyOutcome(score, signals[0][1] if ok else VoiceReason.OTHER,
                             VoicePriority.NORMAL if ok else VoicePriority.LOW, ok)

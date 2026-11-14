"""Vocabulary, token format and parsing. Pure (stdlib only)."""
import hashlib
import os
import re
import secrets
from urllib.parse import quote

VERSION = "acquisition-1"
FLAG = "COPILOT_ACQUISITION_ENABLED"
MARKER = "COPILOTREF"
TOKEN_LEN = 12                                   # secrets.token_urlsafe(9): 72 bits, [A-Za-z0-9_-]
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{%d}$" % TOKEN_LEN)
_MARKER_RE = re.compile(r"COPILOTREF\s*:([A-Za-z0-9_-]*)", re.I)
GREETING = "Hola 👋"                              # the only fixed words of the prefilled draft
FALLBACK_TEXT = "Hola"                           # what remains when a message is nothing but the marker

# click lifecycle: CLICKED -> HANDOFF_GENERATED -> BOUND -> CONVERTED ; side states UNBOUND / INVALID_TOKEN / EXPIRED
CLICKED, HANDOFF_GENERATED, BOUND, CONVERTED = "CLICKED", "HANDOFF_GENERATED", "BOUND", "CONVERTED"
UNBOUND, INVALID_TOKEN, EXPIRED = "UNBOUND", "INVALID_TOKEN", "EXPIRED"
CAMPAIGN_STATUSES = ("active", "paused", "ended")
_NAME_RE = re.compile(r"^[\w .:+/-]{1,64}$")
_DEST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def enabled() -> bool:
    return os.getenv(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def token_ttl_seconds() -> int:
    try:
        v = int(float(os.getenv("COPILOT_ACQ_TOKEN_TTL_SECONDS", "")))
        return v if v > 0 else 3 * 86400
    except ValueError:
        return 3 * 86400


def new_token() -> str:
    return secrets.token_urlsafe(9)


def new_click_id() -> str:
    return "clk_" + secrets.token_urlsafe(12)


def new_campaign_id() -> str:
    return "cmp_" + secrets.token_hex(4)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def valid_label(v) -> bool:
    """source / medium / campaign_name / creative_id: short plain labels, no URLs, emails or handles (PII guard)."""
    return isinstance(v, str) and bool(_NAME_RE.match(v.strip())) and "@" not in v and "http" not in v.lower()


def allowed_destinations() -> frozenset:
    return frozenset(x.strip().lower() for x in os.getenv("COPILOT_ACQ_ALLOWED_DESTINATIONS", "").split(",") if x.strip())


def valid_destination(dest) -> bool:
    """A Telegram username that is on the operator's allowlist. Default allowlist is empty -> nothing is redirectable."""
    return isinstance(dest, str) and bool(_DEST_RE.match(dest)) and dest.lower() in allowed_destinations()


def handoff_url(destination: str, token: str) -> str:
    """t.me/<username>?text=<draft>. The draft = fixed greeting + opaque marker token: no campaign, source, id or PII."""
    return f"https://t.me/{destination}?text=" + quote(f"{GREETING} {MARKER}:{token}", safe="")


def parse_message(text) -> tuple:
    """(token | None, sanitized_text, state). state: none | valid | malformed. EVERY marker occurrence is removed from the text,
    valid or not (the token must never reach the LLM). Only an exact-length first marker binds; duplicates are ignored."""
    s = text if isinstance(text, str) else ""
    found = _MARKER_RE.findall(s)
    if not found:
        return None, s, "none"
    clean = " ".join(_MARKER_RE.sub(" ", s).split()) or FALLBACK_TEXT
    first = found[0]
    return (first, clean, "valid") if TOKEN_RE.match(first) else (None, clean, "malformed")

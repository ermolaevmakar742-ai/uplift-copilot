"""Source adapter contract. Adding a platform/provider = one new class registered in `Registry`; the core never changes.
An adapter returns only what the source really provides: missing metrics stay None (never estimated)."""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

PLATFORMS = ("tiktok", "instagram", "youtube")


class SourceError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass
class VideoRecord:
    platform: str
    url: str
    source: str
    external_id: str | None = None
    title: str | None = None
    author: str | None = None
    author_url: str | None = None
    thumbnail_url: str | None = None
    published_at: datetime | None = None
    duration_s: float | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    followers: int | None = None
    peer_views: list[int] | None = None          # author's other recent videos (for the median baseline)
    language: str | None = None


@dataclass
class SearchParams:
    platform: str = "youtube"
    query: str = ""
    geo: str | None = None
    language: str | None = None
    published_within_days: int | None = 30
    max_duration_s: int | None = 60
    order: str = "views"                          # views | date
    limit: int = 25


@dataclass
class SourceStatus:
    id: str
    name: str
    platforms: list[str]
    state: str                                    # ready | needs_key | metadata_only | not_connected | error
    capabilities: list[str]                       # search | resolve_url | metrics
    requires: str = ""
    note: str = ""
    platform_requires: dict | None = None         # platform -> env var needed before that platform works (e.g. instagram oEmbed token)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class SourceAdapter:
    id = "base"
    name = "Base"
    platforms: tuple = ()

    def status(self) -> SourceStatus:
        raise NotImplementedError

    async def search(self, p: SearchParams) -> list[VideoRecord]:
        raise SourceError("search_unsupported", f"{self.name} does not provide search")

    async def resolve_url(self, url: str) -> VideoRecord | None:
        return None


STATE_LABELS = {"ready": "works", "needs_key": "API key required", "metadata_only": "metadata only (no popularity metrics)", "not_connected": "not connected", "error": "last request failed"}


class Registry:
    def __init__(self):
        self._items: dict[str, SourceAdapter] = {}
        self._errors: dict[str, dict] = {}

    def note_error(self, sid: str, code: str, message: str) -> None:
        import time
        self._errors[sid] = {"code": code, "message": message[:300], "at": time.time()}

    def note_ok(self, sid: str) -> None:
        self._errors.pop(sid, None)

    def register(self, adapter: SourceAdapter) -> None:
        self._items[adapter.id] = adapter

    def get(self, sid: str) -> SourceAdapter | None:
        return self._items.get(sid)

    def all(self) -> list[SourceAdapter]:
        return list(self._items.values())

    def statuses(self) -> list[dict]:
        out = []
        for a in self._items.values():
            d = a.status().as_dict()
            e = self._errors.get(a.id)
            if e and d["state"] in ("ready", "metadata_only"):
                d["last_error"], d["state"] = {"code": e["code"], "message": e["message"]}, "error"
            out.append(d)
        return out

    def coverage(self) -> dict:
        """Per platform, what the connected sources can really deliver: search, popularity metrics, metadata. Never optimistic."""
        cov = {}
        for plat in PLATFORMS:
            row = {"search": {"state": "not_connected", "source": None, "note": "No search source for this platform. Add references by URL or import your own data."},
                   "metrics": {"state": "not_connected", "source": None, "note": "No source returns view/like counts for this platform. Enter measurements manually or import CSV/JSON."},
                   "metadata": {"state": "not_connected", "source": None, "note": ""}}
            for d in self.statuses():
                if plat not in self._items[d["id"]].platforms:
                    continue
                state, requires = d["state"], d.get("requires", "")
                if plat not in d["platforms"] and (d.get("platform_requires") or {}).get(plat):
                    state, requires = "needs_key", d["platform_requires"][plat]
                for cap, key in (("search", "search"), ("metrics", "metrics"), ("resolve_url", "metadata")):
                    if cap in d["capabilities"]:
                        rank = {"ready": 0, "metadata_only": 1, "error": 2, "needs_key": 3, "not_connected": 4}   # usable now beats "would work with a key"
                        if row[key]["source"] is None or rank.get(state, 9) < rank.get(row[key]["state"], 9):
                            row[key] = {"state": state, "source": d["id"], "note": d.get("note") or "", "requires": requires, "last_error": d.get("last_error")}
            cov[plat] = row
        return cov

    def for_platform(self, platform: str, capability: str) -> list[SourceAdapter]:
        return [a for a in self._items.values() if platform in a.platforms and capability in a.status().capabilities and a.status().state in ("ready", "error")]


# ---- URL helpers -------------------------------------------------------------------------------------------------------------------
_HOSTS = {
    "youtube.com": "youtube", "m.youtube.com": "youtube", "www.youtube.com": "youtube", "youtu.be": "youtube",
    "tiktok.com": "tiktok", "www.tiktok.com": "tiktok", "vm.tiktok.com": "tiktok", "vt.tiktok.com": "tiktok", "m.tiktok.com": "tiktok",
    "instagram.com": "instagram", "www.instagram.com": "instagram",
}


def parse_url(url: str) -> tuple[str, str, str | None]:
    """(platform, canonical_url, external_id). Raises SourceError(invalid_url) for non-http(s) or unknown hosts' structure."""
    u = urlparse(str(url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SourceError("invalid_url", "Enter a full http(s) video URL")
    host = u.hostname.lower()
    platform = _HOSTS.get(host, "other")
    ext = None
    if platform == "youtube":
        if host == "youtu.be":
            ext = u.path.strip("/").split("/")[0] or None
        elif u.path.startswith("/shorts/") or u.path.startswith("/embed/") or u.path.startswith("/live/"):
            ext = u.path.split("/")[2] if len(u.path.split("/")) > 2 else None
        else:
            ext = (parse_qs(u.query).get("v") or [None])[0]
        if ext and not re.fullmatch(r"[\w-]{6,20}", ext):
            ext = None
        canon = f"https://www.youtube.com/shorts/{ext}" if ext and "/shorts/" in u.path else (f"https://www.youtube.com/watch?v={ext}" if ext else u.geturl())
    elif platform == "tiktok":
        m = re.search(r"/video/(\d{6,25})", u.path)
        ext = m[1] if m else None
        canon = f"https://{host}{u.path.rstrip('/')}" if ext or host.startswith(("vm.", "vt.")) else u.geturl()
    elif platform == "instagram":
        m = re.search(r"/(?:reel|reels|p|tv)/([\w-]+)", u.path)
        ext = m[1] if m else None
        canon = f"https://www.instagram.com/reel/{ext}/" if ext else u.geturl()
    else:
        canon = f"{u.scheme}://{host}{u.path}" + (f"?{u.query}" if u.query else "")
    return platform, canon, ext


def url_key(canonical: str) -> str:
    return hashlib.sha1(canonical.encode()).hexdigest()

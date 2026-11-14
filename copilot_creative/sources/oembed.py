"""Keyless URL import through the platforms' documented oEmbed endpoints (YouTube, TikTok; Instagram needs a Meta app token).
Gives title/author/thumbnail ONLY: oEmbed exposes no metrics, so views/likes/etc. stay empty. Only these fixed hosts are ever contacted
(the pasted URL is a query parameter, never a fetch target), so there is no SSRF surface."""
import os

import httpx

from copilot_creative.sources.base import SourceAdapter, SourceError, SourceStatus, VideoRecord, parse_url

ENDPOINTS = {"youtube": "https://www.youtube.com/oembed", "tiktok": "https://www.tiktok.com/oembed"}
IG_ENDPOINT = "https://graph.facebook.com/v19.0/instagram_oembed"


class OEmbedSource(SourceAdapter):
    id = "oembed"
    name = "oEmbed URL import"
    platforms = ("youtube", "tiktok", "instagram")

    def __init__(self, env=None, client_factory=None):
        self.env = os.environ if env is None else env
        self._cf = client_factory or (lambda: httpx.AsyncClient(timeout=15, follow_redirects=False))

    def status(self) -> SourceStatus:
        ig = bool(self.env.get("INSTAGRAM_OEMBED_TOKEN"))
        return SourceStatus(self.id, self.name, ["youtube", "tiktok"] + (["instagram"] if ig else []), "metadata_only", ["resolve_url"], platform_requires={"instagram": "INSTAGRAM_OEMBED_TOKEN"},
                            note="Metadata only (title, author, thumbnail): NOT a trend search and no view/like counts. " + ("" if ig else "Instagram needs INSTAGRAM_OEMBED_TOKEN (Meta app token) — otherwise add Instagram references manually."))

    async def resolve_url(self, url: str) -> VideoRecord | None:
        platform, canon, ext = parse_url(url)
        params = {"url": canon, "format": "json"}
        if platform == "instagram":
            tok = self.env.get("INSTAGRAM_OEMBED_TOKEN")
            if not tok:
                return None
            ep, params = IG_ENDPOINT, {"url": canon, "access_token": tok, "fields": "author_name,thumbnail_url"}
        elif platform in ENDPOINTS:
            ep = ENDPOINTS[platform]
        else:
            return None
        try:
            async with self._cf() as c:
                r = await c.get(ep, params=params)
        except httpx.HTTPError as e:
            raise SourceError("source_unreachable", f"{self.name}: network error ({type(e).__name__})")
        if r.status_code in (401, 403, 404):
            raise SourceError("video_unavailable", f"The platform reports this video as private, removed or not embeddable (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise SourceError("source_error", f"{self.name}: HTTP {r.status_code}")
        try:
            d = r.json()
        except ValueError:
            raise SourceError("source_error", f"{self.name}: invalid response")
        return VideoRecord(platform=platform, url=canon, source=self.id, external_id=ext, title=(d.get("title") or None),
                           author=d.get("author_name") or None, author_url=d.get("author_url") or None, thumbnail_url=d.get("thumbnail_url") or None)

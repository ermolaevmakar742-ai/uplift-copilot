"""YouTube Data API v3 (official, free quota; needs YOUTUBE_API_KEY). search.list(100 units) + videos.list/channels.list/playlistItems.list (1 unit each).
The API cannot flag "Shorts": results are short-form videos (duration <= max_duration_s). Real counts only; hidden subscriber counts stay None."""
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from copilot_creative.sources.base import SearchParams, SourceAdapter, SourceError, SourceStatus, VideoRecord, parse_url

API = "https://www.googleapis.com/youtube/v3"
MAX_PEER_CHANNELS = 8


def parse_iso_duration(s: str | None) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


class YouTubeSource(SourceAdapter):
    id = "youtube_api"
    name = "YouTube Data API"
    platforms = ("youtube",)

    def __init__(self, env=None, client_factory=None):
        self.env = os.environ if env is None else env
        self._cf = client_factory or (lambda: httpx.AsyncClient(timeout=20))

    @property
    def key(self) -> str:
        return str(self.env.get("YOUTUBE_API_KEY", "")).strip()

    def status(self) -> SourceStatus:
        if not self.key:
            return SourceStatus(self.id, self.name, ["youtube"], "needs_key", ["search", "resolve_url", "metrics"], requires="YOUTUBE_API_KEY",
                                note="Create an API key in Google Cloud (YouTube Data API v3, free daily quota) and set YOUTUBE_API_KEY in /etc/ares/secrets.env, then restart the service.")
        return SourceStatus(self.id, self.name, ["youtube"], "ready", ["search", "resolve_url", "metrics"],
                            note="Official API. Views/likes/comments and channel subscribers are real; Shorts are identified by duration only. search = 100 quota units.")

    async def _get(self, c, path: str, **params) -> dict:
        try:
            r = await c.get(f"{API}/{path}", params={**params, "key": self.key})
        except httpx.HTTPError as e:
            raise SourceError("source_unreachable", f"YouTube API network error ({type(e).__name__})")
        if r.status_code == 403:
            reason = ""
            try:
                reason = r.json()["error"]["errors"][0]["reason"]
            except Exception:
                pass
            raise SourceError("quota_or_forbidden", f"YouTube API refused the request ({reason or 'HTTP 403'}): quota exhausted or key restricted")
        if r.status_code in (400, 401):
            raise SourceError("bad_key", "YouTube API rejected the key or request (HTTP %d)" % r.status_code)
        if r.status_code >= 400:
            raise SourceError("source_error", f"YouTube API HTTP {r.status_code}")
        return r.json()

    def _record(self, it: dict, subs: dict) -> VideoRecord:
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        vid = it["id"]
        return VideoRecord(platform="youtube", url=f"https://www.youtube.com/watch?v={vid}", source=self.id, external_id=vid,
                           title=sn.get("title"), author=sn.get("channelTitle"), author_url=f"https://www.youtube.com/channel/{sn.get('channelId')}" if sn.get("channelId") else None,
                           thumbnail_url=((sn.get("thumbnails") or {}).get("high") or (sn.get("thumbnails") or {}).get("default") or {}).get("url"),
                           published_at=_dt(sn.get("publishedAt")), duration_s=parse_iso_duration((it.get("contentDetails") or {}).get("duration")),
                           views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")), comments=_int(st.get("commentCount")), shares=None,
                           followers=subs.get(sn.get("channelId")), language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"))

    async def _videos(self, c, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            out += (await self._get(c, "videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))).get("items", [])
        return out

    async def _subs(self, c, channel_ids: list[str]) -> dict:
        subs = {}
        for i in range(0, len(channel_ids), 50):
            for ch in (await self._get(c, "channels", part="statistics", id=",".join(channel_ids[i:i + 50]))).get("items", []):
                s = ch.get("statistics", {})
                subs[ch["id"]] = None if s.get("hiddenSubscriberCount") else _int(s.get("subscriberCount"))
        return subs

    async def _peers(self, c, channel_id: str) -> list[tuple[str, int]]:
        """(video_id, views) of the channel's latest uploads; callers drop the video under analysis so it never inflates its own baseline."""
        uploads = "UU" + channel_id[2:] if channel_id.startswith("UC") else None
        if not uploads:
            return []
        items = (await self._get(c, "playlistItems", part="contentDetails", playlistId=uploads, maxResults=25)).get("items", [])
        ids = [i["contentDetails"]["videoId"] for i in items]
        vids = await self._videos(c, ids[:25]) if ids else []
        return [(x["id"], v) for x in vids if (v := _int((x.get("statistics") or {}).get("viewCount"))) is not None]

    async def search(self, p: SearchParams) -> list[VideoRecord]:
        if not self.key:
            raise SourceError("needs_key", "YOUTUBE_API_KEY is not configured")
        if not p.query.strip():
            raise SourceError("query_required", "Enter keywords to search")
        q = {"part": "snippet", "type": "video", "q": p.query, "maxResults": min(50, max(1, p.limit)), "order": "viewCount" if p.order == "views" else "date",
             "videoDuration": "short"}                                       # < 4 min; exact limit applied below
        if p.geo:
            q["regionCode"] = p.geo.upper()[:2]
        if p.language:
            q["relevanceLanguage"] = p.language.lower()[:2]
        if p.published_within_days:
            q["publishedAfter"] = (datetime.now(timezone.utc) - timedelta(days=p.published_within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self._cf() as c:
            ids = [i["id"]["videoId"] for i in (await self._get(c, "search", **q)).get("items", []) if i.get("id", {}).get("videoId")]
            items = await self._videos(c, ids) if ids else []
            if p.max_duration_s:
                items = [i for i in items if (parse_iso_duration((i.get("contentDetails") or {}).get("duration")) or 10 ** 9) <= p.max_duration_s]
            subs = await self._subs(c, sorted({i["snippet"]["channelId"] for i in items if i.get("snippet", {}).get("channelId")}))
            recs = [self._record(i, subs) for i in items]
            peers: dict[str, list[tuple[str, int]]] = {}
            for ch in list(dict.fromkeys(i["snippet"]["channelId"] for i in items))[:MAX_PEER_CHANNELS]:
                try:
                    peers[ch] = await self._peers(c, ch)
                except SourceError:
                    peers[ch] = []
            for r, i in zip(recs, items):
                r.peer_views = [v for vid, v in peers.get(i["snippet"]["channelId"], []) if vid != r.external_id] or None
        return recs

    async def resolve_url(self, url: str) -> VideoRecord | None:
        if not self.key:
            return None
        platform, _, ext = parse_url(url)
        if platform != "youtube" or not ext:
            return None
        async with self._cf() as c:
            items = await self._videos(c, [ext])
            if not items:
                raise SourceError("video_unavailable", "YouTube reports no such public video")
            subs = await self._subs(c, [items[0]["snippet"]["channelId"]])
            rec = self._record(items[0], subs)
            try:
                rec.peer_views = [v for vid, v in await self._peers(c, items[0]["snippet"]["channelId"]) if vid != ext] or None
            except SourceError:
                pass
        return rec

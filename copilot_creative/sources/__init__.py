from copilot_creative.sources.base import Registry, SearchParams, SourceAdapter, SourceError, SourceStatus, VideoRecord, parse_url, url_key  # noqa: F401


def default_registry(env=None) -> Registry:
    from copilot_creative.sources.oembed import OEmbedSource
    from copilot_creative.sources.youtube import YouTubeSource
    r = Registry()
    r.register(YouTubeSource(env))
    r.register(OEmbedSource(env))
    return r

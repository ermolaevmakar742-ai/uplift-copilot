"""Creative Intelligence tables (own MetaData; touch NO existing table). Schema only: DDL is applied by migrate.py (CLI) or lazily
and idempotently by the router (CREATE ... IF NOT EXISTS). JSON columns are Text for portability (PostgreSQL + SQLite tests)."""
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text, func)

metadata = MetaData()
PK = BigInteger().with_variant(Integer(), "sqlite")

# One row per video (discovered by search, added by URL, imported or uploaded). saved=true => it is in "My Creative Library".
cr_references = Table(
    "cr_references", metadata,
    Column("id", PK, primary_key=True, autoincrement=True),
    Column("url_key", String(80), nullable=False, unique=True),     # sha1 of normalised URL, or "file:<sha256>" for uploads without URL
    Column("platform", String(24), nullable=False),                 # tiktok | instagram | youtube | other
    Column("url", Text),
    Column("external_id", String(64)),
    Column("title", Text),
    Column("author", String(200)),
    Column("author_url", Text),
    Column("thumbnail_url", Text),
    Column("published_at", DateTime(timezone=True)),
    Column("duration_s", Float),
    Column("niche", String(120)),
    Column("geo", String(16)),
    Column("language", String(16)),
    Column("keywords", Text),                                       # the search query that discovered it
    Column("video_type", String(40)),
    Column("delivery", String(40)),
    Column("trend_status", String(24), nullable=False, server_default="new"),
    Column("production_status", String(24), nullable=False, server_default="idea"),
    Column("tags", Text, nullable=False, server_default="[]"),      # JSON list
    Column("notes", Text),
    Column("source", String(40), nullable=False),                   # adapter id that supplied the metadata
    Column("peer_views", Text),                                     # JSON {"views":[...], "captured_at": iso, "source": id}: the author's other videos
    Column("saved", Boolean, nullable=False, server_default="0"),
    Column("discovered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_cr_refs_platform", "platform", "discovered_at"),
    Index("ix_cr_refs_saved", "saved", "updated_at"),
)

# Append-only metric snapshots. NULL = not available from the source (never an estimate).
cr_metrics = Table(
    "cr_metrics", metadata,
    Column("id", PK, primary_key=True, autoincrement=True),
    Column("ref_id", BigInteger, ForeignKey("cr_references.id", ondelete="CASCADE"), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("views", BigInteger), Column("likes", BigInteger), Column("comments", BigInteger), Column("shares", BigInteger),
    Column("followers", BigInteger),
    Column("source", String(40), nullable=False),
    Column("kind", String(16), nullable=False, server_default="source"),   # source | manual
    Index("ix_cr_metrics_ref", "ref_id", "captured_at"),
)

# Video analysis jobs. queued -> processing -> completed | failed. The report is stored, so reopening never re-bills.
cr_analyses = Table(
    "cr_analyses", metadata,
    Column("id", PK, primary_key=True, autoincrement=True),
    Column("ref_id", BigInteger, ForeignKey("cr_references.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("error", Text),
    Column("input_sha256", String(64), nullable=False),            # hash of the video file => dedupe
    Column("upload_path", Text),                                   # kept only until completed (retry) or TTL
    Column("model", String(80)),
    Column("frames", Text),                                        # JSON [{"n","t","file"}]
    Column("facts", Text),                                         # JSON: ffmpeg-derived, confirmed characteristics
    Column("report", Text),                                        # JSON: model analysis (observations vs inferences)
    Column("log", Text, nullable=False, server_default="[]"),      # JSON list of {"t","msg"}
    Column("usage", Text),                                         # JSON token usage reported by the provider
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_cr_analyses_ref", "ref_id", "created_at"),
    Index("ix_cr_analyses_hash", "ref_id", "input_sha256"),
)

# Prompt Forge results (also jobs). params_hash + analysis_id => cached, regenerate on demand.
cr_prompts = Table(
    "cr_prompts", metadata,
    Column("id", PK, primary_key=True, autoincrement=True),
    Column("ref_id", BigInteger, ForeignKey("cr_references.id", ondelete="CASCADE"), nullable=False),
    Column("analysis_id", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("error", Text),
    Column("params", Text, nullable=False),
    Column("params_hash", String(64), nullable=False),
    Column("model", String(80)),
    Column("result", Text),
    Column("usage", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_cr_prompts_ref", "ref_id", "created_at"),
    Index("ix_cr_prompts_hash", "ref_id", "analysis_id", "params_hash"),
)

# Results of the user's own publications made from a reference.
cr_publications = Table(
    "cr_publications", metadata,
    Column("id", PK, primary_key=True, autoincrement=True),
    Column("ref_id", BigInteger, ForeignKey("cr_references.id", ondelete="CASCADE"), nullable=False),
    Column("url", Text), Column("platform", String(24)), Column("posted_at", DateTime(timezone=True)),
    Column("views", BigInteger), Column("likes", BigInteger), Column("comments", BigInteger), Column("shares", BigInteger),
    Column("notes", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_cr_pubs_ref", "ref_id"),
)

TABLES = (cr_references, cr_metrics, cr_analyses, cr_prompts, cr_publications)
ROLLBACK_DDL = tuple(f"DROP TABLE IF EXISTS {t.name}" for t in reversed(TABLES))

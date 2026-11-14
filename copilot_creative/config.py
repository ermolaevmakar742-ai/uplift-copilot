"""Creative Intelligence configuration. Read from an env MAPPING at call time (nothing at import); every limit is overridable.

  COPILOT_CREATIVE_MAX_UPLOAD_MB (150)   COPILOT_CREATIVE_MAX_DURATION_S (180)   COPILOT_CREATIVE_MAX_FRAMES (12)   COPILOT_CREATIVE_FRAME_WIDTH (768)
  COPILOT_CREATIVE_TRANSCRIPT_CHARS (6000)   COPILOT_CREATIVE_VISION_MODEL (default: COPILOT_CREATIVE_VISION_MODEL or the panel's analyst model)
  COPILOT_CREATIVE_TEXT_MODEL (default = vision model)   COPILOT_CREATIVE_DATA_DIR (<repo>/data/creative)   COPILOT_CREATIVE_UPLOAD_TTL_H (24)
  COPILOT_CREATIVE_STT_URL / _KEY / _MODEL   optional OpenAI-compatible /audio/transcriptions provider; unset => transcript reported as unavailable
  YOUTUBE_API_KEY   enables the YouTube Data API source          INSTAGRAM_OEMBED_TOKEN   enables Instagram oEmbed metadata
The LLM endpoint/key are the panel's existing LLM_BASE_URL / LLM_API_KEY (same OpenRouter account as the bot)."""
import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_VISION_MODEL = "anthropic/claude-opus-5"      # LLM_MODEL_ANALYST in main.py; image input verified in the OpenRouter catalogue during the audit
ALLOWED_EXT = {".mp4": "iso", ".mov": "iso", ".m4v": "iso", ".webm": "ebml", ".mkv": "ebml"}


def _int(env, name, default, lo, hi):
    try:
        v = int(str(env.get(name, "")).strip())
    except ValueError:
        return default
    return min(hi, max(lo, v))


@dataclass(frozen=True)
class Config:
    max_upload_mb: int
    max_duration_s: int
    max_frames: int
    frame_width: int
    transcript_chars: int
    vision_model: str
    text_model: str
    data_dir: str
    upload_ttl_h: int
    llm_base: str
    stt_url: str
    stt_model: str

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load(env: Mapping | None = None) -> Config:
    e = os.environ if env is None else env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vision = (e.get("COPILOT_CREATIVE_VISION_MODEL") or DEFAULT_VISION_MODEL).strip()
    return Config(
        max_upload_mb=_int(e, "COPILOT_CREATIVE_MAX_UPLOAD_MB", 150, 1, 2000),
        max_duration_s=_int(e, "COPILOT_CREATIVE_MAX_DURATION_S", 180, 3, 1800),
        max_frames=_int(e, "COPILOT_CREATIVE_MAX_FRAMES", 12, 3, 32),
        frame_width=_int(e, "COPILOT_CREATIVE_FRAME_WIDTH", 768, 256, 1536),
        transcript_chars=_int(e, "COPILOT_CREATIVE_TRANSCRIPT_CHARS", 6000, 500, 40000),
        vision_model=vision,
        text_model=(e.get("COPILOT_CREATIVE_TEXT_MODEL") or vision).strip(),
        data_dir=(e.get("COPILOT_CREATIVE_DATA_DIR") or os.path.join(here, "data", "creative")).strip(),
        upload_ttl_h=_int(e, "COPILOT_CREATIVE_UPLOAD_TTL_H", 24, 1, 720),
        llm_base=(e.get("LLM_BASE_URL") or "").strip(),
        stt_url=(e.get("COPILOT_CREATIVE_STT_URL") or "").strip(),
        stt_model=(e.get("COPILOT_CREATIVE_STT_MODEL") or "").strip(),
    )

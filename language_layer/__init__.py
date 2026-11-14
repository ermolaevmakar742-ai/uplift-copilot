"""COPILOT language layer: GEO-aware localization of Valeria's replies.

Flow: intent -> Valeria response (existing LLM call, steered by a LANGUAGE POLICY block)
      -> deterministic validation / safe cleanup / meaning guard -> Telegram.
Default state is inert (LANGUAGE_LAYER=false). See config.py for flags.
"""
from .config import LanguageConfig, get_config
from .models import GeoProfile, LocalizationRequest, LocalizationResult
from .service import apply_prompt, localize, process_reply
from . import registry

__all__ = ["LanguageConfig", "get_config", "GeoProfile", "LocalizationRequest", "LocalizationResult",
           "apply_prompt", "localize", "process_reply", "registry"]

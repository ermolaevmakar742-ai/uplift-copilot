"""Importing this package registers all built-in profiles.
Add a locale: create profiles/es_ar.py (derive from ES_BASE or another profile), import it here."""
from . import es_mx, es_co, es_pe, es_ec, pt_br  # noqa: F401

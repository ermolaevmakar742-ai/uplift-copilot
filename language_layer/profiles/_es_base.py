"""Shared Latin-American Spanish base. Locale profiles derive from it."""
from ..models import ForbiddenRule, GeoProfile

# Obvious Spain-isms and corporate / translated-sounding constructions.
# They are FLAGGED by the validator, never auto-rewritten.
ES_FORBIDDEN = (
    ForbiddenRule("spain_vosotros", r"\b(vosotros|vosotras|habéis|queréis|podéis)\b", "Spain 2nd person plural"),
    ForbiddenRule("spain_vale", r"(?:^|[\s¡¿,])vale(?=[,.!?]|$)", "'vale' as OK is Peninsular"),
    ForbiddenRule("spain_slang", r"\b(tío|tía|tronco|mola|molar|flipar|guay)\b", "Peninsular slang"),
    ForbiddenRule("spain_tech", r"\b(ordenador|móvil)\b", "Peninsular words (computadora/celular in LATAM)"),
    ForbiddenRule("corp_no_dude", r"\bno dude en\b", "customer-service script"),
    ForbiddenRule("corp_disposicion", r"\bquedo a (?:su|tu) disposición\b", "customer-service script"),
    ForbiddenRule("corp_estimado", r"\bestimad[oa]s? (?:cliente|usuari[oa])\b", "formal letter register"),
    ForbiddenRule("corp_continuacion", r"\ba continuación\b", "bureaucratic connector"),
    ForbiddenRule("corp_importante", r"\bes importante (?:mencionar|destacar|señalar|notar)\b", "translated essay register"),
    ForbiddenRule("corp_ayudar", r"\ben qu[eé] puedo ayudar(?:te|le)\b|\bestoy aquí para ayudar(?:te|le)\b", "assistant cliche"),
    ForbiddenRule("calque_sentido", r"\bhacer sentido\b|\bhace sentido\b", "calque of 'make sense' (tiene sentido)"),
    ForbiddenRule("calque_llamar", r"\bllamar de vuelta\b|\bllamarte de vuelta\b", "calque of 'call back'"),
    ForbiddenRule("calque_final", r"\ben el final del día\b", "calque of 'at the end of the day'"),
    ForbiddenRule("calque_excitado", r"\bestoy (?:muy )?excitad[oa]\b", "false friend of 'excited'"),
)

ES_BASE = GeoProfile(
    code="es-XX",
    language="es",
    label="Latin American Spanish (base)",
    informal_pronoun="tú",
    formal_pronoun="usted",
    forbidden=ES_FORBIDDEN,
    tone_rules=(
        "WhatsApp rhythm: short, spoken sentences, one idea at a time.",
        "No customer-service politeness; at most one 'gracias' or 'por favor' per message.",
        "Do not open every message with the same word or the same greeting.",
        "Prefer contractions of speech ('pa', 'pues', 'ya') only when natural for the locale; never force slang.",
    ),
)

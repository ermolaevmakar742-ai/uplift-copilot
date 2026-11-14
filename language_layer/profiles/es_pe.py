from ..models import ForbiddenRule
from ..registry import register
from ._es_base import ES_BASE

ES_PE = register(ES_BASE.derive(
    code="es-PE",
    label="Español del Perú",
    country_codes=("PE", "PERU", "PERÚ"),
    address_rule="'tú' by default; 'usted' with older or very formal clients. Mirror the client.",
    vocabulary={
        "money": "plata / lucas", "cell phone": "celular", "work": "chamba",
        "right away": "al toque", "great": "chévere / bacán", "friend": "pata / causa (only if the client uses it)",
    },
    patterns=("Ya, listo", "Sí pe", "Dime nomás", "¿Te parece?", "Claro, ahí vemos"),
    forbidden=ES_BASE.forbidden + (
        ForbiddenRule("pe_mexicanism", r"\b(órale|orale|güey|wey|no manches|ándale|híjole)\b", "Mexican interjections"),
    ),
    cta={
        "invite": ("¿Te animas?", "¿Lo probamos?", "¿Qué dices?"),
        "register": ("Regístrate aquí nomás", "Crea tu cuenta aquí"),
        "continue": ("Ya, sigamos", "Dime nomás"),
    },
    tone_rules=ES_BASE.tone_rules + (
        "'pe' and 'nomás' are natural but must be rare (at most once per few messages); do not caricature.",
    ),
    examples=("Ya, cuéntame, ¿cómo vas?", "Dime nomás y lo vemos.", "¿Te parece si lo probamos?"),
    lexical_markers={
        "al toque": 1.0, "pata": 0.4, "bacán": 0.6, "jato": 1.0, "chibolo": 1.0,
        "nomás": 0.3, "lucas": 0.3, "chamba": 0.3, "pe": 0.3,
    },
))

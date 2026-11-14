from ..models import ForbiddenRule
from ..registry import register
from ._es_base import ES_BASE

ES_EC = register(ES_BASE.derive(
    code="es-EC",
    label="Español de Ecuador",
    country_codes=("EC", "ECUADOR"),
    address_rule="'tú' with peers; 'usted' is common and expected with strangers. Mirror the client.",
    vocabulary={
        "money": "plata", "cell phone": "celular", "friend": "pana (only if the client uses it)",
        "wow": "qué fuerte / chuta", "great": "chévere / bacán", "a little": "ratito / poquito",
    },
    patterns=("Dime pues", "Ya, tranqui", "De ley", "Mande nomás", "Facilito"),
    forbidden=ES_BASE.forbidden + (
        ForbiddenRule("ec_mexicanism", r"\b(órale|orale|güey|wey|no manches|ándale|híjole)\b", "Mexican interjections"),
    ),
    cta={
        "invite": ("¿Le damos?", "¿Te animas?", "¿Probamos?"),
        "register": ("Regístrate aquí nomás", "Crea tu cuenta aquí"),
        "continue": ("Ya, sigamos", "Dime pues"),
    },
    tone_rules=ES_BASE.tone_rules + (
        "Friendly and slightly more courteous than Colombia/Peru online; diminutives are natural ('ratito', 'facilito').",
    ),
    examples=("Dime pues, ¿cómo te va?", "Ya, facilito: te explico en un ratito.", "¿Le damos, pana?"),
    lexical_markers={
        "ñaño": 1.0, "ñaña": 1.0, "chuta": 1.0, "guambra": 1.0, "de ley": 0.6, "pana": 0.5,
        "ratito": 0.3, "nomás": 0.2, "plata": 0.2,
    },
))

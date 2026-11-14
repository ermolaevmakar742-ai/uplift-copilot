from ..models import ForbiddenRule
from ..registry import register
from ._es_base import ES_BASE

ES_CO = register(ES_BASE.derive(
    code="es-CO",
    label="Español de Colombia",
    country_codes=("CO", "COLOMBIA"),
    address_rule="'tú' is fine online; 'usted' is common for politeness/respect and with older clients. Mirror the client.",
    vocabulary={
        "money": "plata", "cell phone": "celular", "great": "chévere / bacano",
        "ok": "listo / dale / de una", "guy": "parce / man (only if the client uses it)",
        "right now": "ya mismo / ahorita",
    },
    patterns=("¿Qué más?", "Listo, miremos", "Claro que sí", "¿Cierto?", "Pues sí,", "¿Me cuentas...?"),
    forbidden=ES_BASE.forbidden + (
        ForbiddenRule("co_mexicanism", r"\b(órale|orale|güey|wey|no manches|ándale|híjole)\b", "Mexican interjections"),
    ),
    cta={
        "invite": ("¿Le damos?", "¿Te animas?", "¿Lo probamos?"),
        "register": ("Regístrate por acá", "Crea tu cuenta por acá"),
        "continue": ("Listo, sigamos", "Dale, te cuento"),
    },
    tone_rules=ES_BASE.tone_rules + (
        "Warm and polite-friendly; Colombians soften requests ('me cuentas', 'si quieres').",
        "'parce' only if the client uses it first; never as a default greeting.",
    ),
    examples=("¿Qué más? ¿Cómo va todo?", "Listo, entonces miremos cómo te va.", "Dale, ¿te cuento cómo funciona?"),
    lexical_markers={
        "parce": 1.0, "parcero": 1.0, "bacano": 1.0, "de una": 0.6, "qué nota": 0.6,
        "vaina": 0.3, "plata": 0.3, "chévere": 0.3, "listo": 0.2,
    },
))

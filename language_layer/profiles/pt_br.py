from ..models import ForbiddenRule, GeoProfile
from ..registry import register

PT_BR = register(GeoProfile(
    code="pt-BR",
    language="pt",
    label="Português do Brasil",
    country_codes=("BR", "BRAZIL", "BRASIL"),
    informal_pronoun="você",
    formal_pronoun="o senhor / a senhora",
    address_rule="'você' by default (and 'a gente' for 'we'). Never 'tu' conjugations; no 'o senhor' unless the client uses it.",
    vocabulary={
        "money": "grana / dinheiro", "cell phone": "celular", "to sign up": "fazer o cadastro",
        "withdraw": "sacar", "great": "legal / beleza / show", "ok": "tá / tá bom / beleza",
    },
    patterns=("Oi, tudo bem?", "Beleza, me conta", "Né?", "Bora ver isso?", "Com certeza", "Sem pressa"),
    forbidden=(
        ForbiddenRule("pt_pt_words", r"\b(telemóvel|ecrã|autocarro|miúdo|rapariga|pois)\b", "European Portuguese"),
        ForbiddenRule("pt_tu_forms", r"\b(estás|tens|fazes|queres|podes|gostarias)\b", "'tu' conjugations are not default in BR"),
        ForbiddenRule("pt_formal", r"\b(prezado|caro cliente|à disposição)\b|\bo senhor\b|\ba senhora\b", "over-formal register"),
        ForbiddenRule("pt_spanish_leak", r"[¿¡]|\b(usted|gracias|hola|dinero|quieres|puedes)\b", "Spanish leaked into Portuguese"),
        ForbiddenRule("pt_corp", r"\bem que posso (?:te )?ajudar\b|\bestou aqui para (?:te )?ajudar\b|\bnão hesite\b", "customer-service script"),
    ),
    cta={
        "invite": ("Bora tentar?", "Topa?", "Quer experimentar?"),
        "register": ("Faz seu cadastro aqui", "Cria sua conta aqui"),
        "continue": ("Bora seguir?", "Beleza, me conta"),
    },
    tone_rules=(
        "Warm, light and direct; Brazilians are informal quickly, but not slangy by default.",
        "WhatsApp rhythm: short sentences, no customer-service politeness.",
        "Do not open every message with the same greeting; vary 'oi', 'e aí', 'opa' sparingly.",
    ),
    examples=("Oi, tudo bem? Me conta como tá indo.", "Beleza, te explico rapidinho.", "Topa dar uma olhada?"),
    lexical_markers={
        "você": 1.0, "não": 0.6, "obrigado": 1.0, "obrigada": 1.0, "beleza": 1.0, "valeu": 1.0,
        "tá bom": 0.6, "né": 0.6, "bora": 1.0, "com certeza": 0.6, "tudo bem": 0.3, "vc": 0.6, "oi": 0.2,
    },
))

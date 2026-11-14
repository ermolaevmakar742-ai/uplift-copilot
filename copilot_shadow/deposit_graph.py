"""Deposit Path Graph: where the client is on a NON-linear path, and (only
when observable evidence exists) which forward transition looks blocked.

Nodes START INTEREST NEED TRUST OBJECTION PAYMENT DEPOSIT SUCCESS DROPPED.
The graph allows backward moves (PAYMENT→TRUST, OBJECTION→NEED, ...).
blocked_transition/blocker are INFERENCES: reported with evidence and a
rule-coverage confidence; null when there is no evidence. Nothing is invented.
"""
from copilot_shadow import signals as S

VERSION = "deposit-graph-1"

NODES = ("START", "INTEREST", "NEED", "TRUST", "OBJECTION", "PAYMENT", "DEPOSIT", "SUCCESS", "DROPPED")
FORWARD = {"START": "INTEREST", "INTEREST": "NEED", "NEED": "TRUST", "TRUST": "PAYMENT",
           "PAYMENT": "DEPOSIT", "DEPOSIT": "SUCCESS"}
RANK = {"START": 0, "INTEREST": 1, "NEED": 2, "TRUST": 3, "PAYMENT": 4, "DEPOSIT": 5, "SUCCESS": 6}
EDGES = {
    "START": ("INTEREST", "DROPPED"),
    "INTEREST": ("NEED", "TRUST", "OBJECTION", "DROPPED"),
    "NEED": ("TRUST", "PAYMENT", "OBJECTION", "INTEREST", "DROPPED"),
    "TRUST": ("PAYMENT", "OBJECTION", "NEED", "INTEREST", "DROPPED"),
    "OBJECTION": ("NEED", "TRUST", "INTEREST", "PAYMENT", "DROPPED"),
    "PAYMENT": ("DEPOSIT", "TRUST", "OBJECTION", "NEED", "DROPPED"),
    "DEPOSIT": ("SUCCESS", "PAYMENT", "OBJECTION", "DROPPED"),
    "SUCCESS": (),
    "DROPPED": ("INTEREST",),   # production has a 'reactivated' stage
}
# production users.stage -> node (used only as a fallback / terminal fact)
STAGE_NODE = {"new": "START", "visited": "START", "conversation": "INTEREST", "qualified": "NEED",
              "offer": "PAYMENT", "action": "PAYMENT", "objection": "OBJECTION",
              "hesitation": "OBJECTION", "blocked": "OBJECTION", "deposited": "SUCCESS",
              "converted": "SUCCESS", "lost": "DROPPED", "reactivated": "INTEREST"}

# (flag on F, blocker name, applies when furthest/current rank >= , blocker evidence signal)
_BLOCKERS = (
    ("tech", "TECHNICAL_ISSUE", 0),
    ("pay_method", "PAYMENT_METHOD_UNCERTAINTY", RANK["PAYMENT"]),
    ("pay_amount", "PAYMENT_AMOUNT_HESITATION", RANK["PAYMENT"]),
    ("trust", "TRUST_CONCERN", 0),
    ("deferral", "DEFERRAL", 0),
)


def turn_node(f) -> str | None:
    if f.role != "user":
        return None
    if f.deposit_done:
        return "DEPOSIT"
    if f.trust:
        return "TRUST"
    if f.payment or f.pay_amount or f.pay_method:
        return "PAYMENT"
    if f.refusal or f.deferral:
        return "OBJECTION"
    if f.need:
        return "NEED"
    if f.interest or f.value or f.question:
        return "INTEREST"
    return None


def _empty(why: str) -> dict:
    return {"version": VERSION, "current_node": "START", "previous_node": None,
            "possible_next_nodes": list(EDGES["START"]), "furthest_node": None,
            "regression": False, "blocked_transition": None, "blocker": None,
            "blocker_confidence": None, "transition_confidence": 0.0,
            "confidence_type": S.CONFIDENCE_TYPE, "node_source": "no_evidence",
            "evidence": [], "note": why}


def build(fs: list, stage: str | None) -> dict:
    ui = S.last_user_index(fs)
    stage_node = STAGE_NODE.get((stage or "").strip().lower())
    seq = [(i, turn_node(f)) for i, f in enumerate(fs) if f.role == "user"]
    seq = [(i, n) for i, n in seq if n]

    if stage_node in ("SUCCESS", "DROPPED"):
        current, conf, source = stage_node, 0.9, "production_stage"
    elif seq and seq[-1][0] == ui:
        current, conf, source = seq[-1][1], 0.8, "current_turn_text"
    elif seq:
        current, conf, source = seq[-1][1], 0.5, "earlier_turn_text"
    elif stage_node:
        current, conf, source = stage_node, 0.4, "production_stage"
    else:
        return _empty("no_evidence")

    previous = next((n for _, n in reversed(seq) if n != current), None)
    on_path = [n for _, n in seq if n in RANK] + ([current] if current in RANK else [])
    furthest = max(on_path, key=lambda n: RANK[n]) if on_path else None
    regression = bool(furthest and (current == "OBJECTION" or
                                    (current in RANK and RANK[current] < RANK[furthest])))
    nxt = list(EDGES[current])
    if current in FORWARD and FORWARD[current] in nxt:
        nxt.remove(FORWARD[current])
        nxt.insert(0, FORWARD[current])

    blocker = bconf = blocked = None
    evidence: list = []
    if conf >= 0.5 and current not in ("SUCCESS", "DROPPED") and furthest in FORWARD:
        # only evidence from the moment the furthest node was reached (older
        # evidence, e.g. a payment-method question before DEPOSIT, is resolved)
        first = next((i for i, n in seq if n == furthest), ui)
        recent = [i for i, f in enumerate(fs) if f.role == "user" and i >= first][-3:]
        best = None
        for flag, name, min_rank in _BLOCKERS:
            if RANK.get(furthest, 0) < min_rank:
                continue
            hits = [i for i in recent if getattr(fs[i], flag)]
            if not hits:
                continue
            c = 0.5 + (0.2 if ui in hits else 0.0) + 0.1 * (len(hits) - 1)
            c = round(min(0.8, c), 2)
            if best is None or c > best[1]:
                best = (name, c, [S.ev(fs[i], flag) for i in hits])
        if best:
            blocker, bconf, evidence = best
            blocked = f"{furthest}→{FORWARD[furthest]}"

    out = {
        "version": VERSION, "current_node": current, "previous_node": previous,
        "possible_next_nodes": nxt, "furthest_node": furthest, "regression": regression,
        "blocked_transition": blocked, "blocker": blocker, "blocker_confidence": bconf,
        "transition_confidence": conf, "confidence_type": S.CONFIDENCE_TYPE,
        "node_source": source, "evidence": evidence,
    }
    if previous:
        out["observed_transition"] = f"{previous}→{current}"
    return out

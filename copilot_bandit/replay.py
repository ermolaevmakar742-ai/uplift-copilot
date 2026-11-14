"""Rebuild bandit training samples from Shadow Store rows (the store is the source of truth; the model is disposable).

Only rows whose reward is defined, whose production action is CANONICAL (mapping status MAPPED, an actions.v1 id) and whose
stored features_ref matches the context recomputed from the stored PII-free snapshot become samples. A row written before the
mapping fields existed may carry a caller-joined `ledger_meta`; it is mapped by the SAME function the live path uses
(research.map_production -> action_mapping.map_decision v2). Everything else is skipped, never guessed."""
from copilot_bandit.features import encode, feature_vector, features_ref
from copilot_bandit.research import map_production


def canonical_action_of(row):
    if row.get("action_mapping_status") is not None:
        return row.get("canonical_action") if row["action_mapping_status"] == "MAPPED" else None
    return map_production(row.get("ledger_meta"))["canonical_action"]


def samples_from_rows(rows):
    for r in rows:
        try:
            if r.get("reward") is None:
                continue
            action = canonical_action_of(r)
            if action is None:
                continue
            fv = feature_vector(r["decision_id"], r["chat_id"], r.get("state_snapshot"))
            if r.get("features_ref") != features_ref(fv):
                continue
            yield encode(fv), action, float(r["reward"])
        except Exception:
            continue

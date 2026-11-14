import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from copilot_evidence.contracts import ContractError
from copilot_uplift.actions import (BASELINE_ACTION_ID, DEFAULT_REGISTRY, Action, ActionRegistry, UnknownAction, action_for_regime)
from copilot_uplift.features import (ALLOWED_SOURCES, FEATURE_IDS, FEATURE_VERSION, Feature, FeatureVector, LeakageError,
                                  audit_vectors, build_features)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class RegistryTests(unittest.TestCase):
    def test_initial_action_space_and_stable_ids(self):
        self.assertEqual(set(DEFAULT_REGISTRY.ids()), {"DO_NOTHING", "ASK_QUESTION", "BUILD_TRUST", "ADDRESS_OBJECTION", "CLARIFY_PAYMENT",
                                                        "REGISTRATION_GUIDANCE", "DEPOSIT_GUIDANCE", "FOLLOW_UP", "HUMAN_HANDOFF"})
        for aid in DEFAULT_REGISTRY.ids():
            a = DEFAULT_REGISTRY.get(aid)
            self.assertEqual((a.action_id, bool(a.description), bool(a.version)), (aid, True, True))
        self.assertTrue(DEFAULT_REGISTRY.get(BASELINE_ACTION_ID).baseline)
        self.assertEqual(DEFAULT_REGISTRY.non_baseline_ids(), tuple(sorted(set(DEFAULT_REGISTRY.ids()) - {"DO_NOTHING"})))

    def test_unknown_action_and_identity_is_not_free_text(self):
        for bad in ("do nothing", "unknown", "", None, 5, "ask_question"):
            self.assertNotIn(bad, DEFAULT_REGISTRY)
        with self.assertRaises(UnknownAction):
            DEFAULT_REGISTRY.get("MAKE_THEM_PAY")
        for bad in ("lower", "Has Space", "", "A-B", "X;DROP"):
            with self.assertRaises(ValueError):
                Action(bad, "desc")
        with self.assertRaises(ValueError):
            Action("OK_ID", "  ")

    def test_extensible_without_mutation(self):
        ext = DEFAULT_REGISTRY.extended(Action("NEW_ACTION", "d"), version="actions.v2")
        self.assertIn("NEW_ACTION", ext)
        self.assertNotIn("NEW_ACTION", DEFAULT_REGISTRY)
        self.assertEqual((ext.version, DEFAULT_REGISTRY.version), ("actions.v2", "actions.v1"))
        with self.assertRaises(ValueError):
            DEFAULT_REGISTRY.extended(Action("ASK_QUESTION", "dup"), version="x")
        with self.assertRaises(ValueError):
            DEFAULT_REGISTRY.extended(Action("SECOND_BASE", "d", baseline=True), version="x")
        with self.assertRaises(TypeError):
            DEFAULT_REGISTRY._items["X"] = 1

    def test_production_mapping_is_explicit_and_partial(self):
        self.assertEqual(action_for_regime("hard"), ("ADDRESS_OBJECTION", "mapped"))
        self.assertEqual(action_for_regime(" HARD ")[0], "ADDRESS_OBJECTION")
        for r in ("agent", "casual", "retention"):
            a, why = action_for_regime(r)
            self.assertIsNone(a)
            self.assertTrue(why.startswith("regime_unmapped:"))
        self.assertEqual(action_for_regime("weird")[0], None)
        self.assertEqual(action_for_regime(None), (None, "regime_unknown"))
        self.assertIsNone(action_for_regime("hard", ActionRegistry([Action("ASK_QUESTION", "d")]))[0])


class FeatureTests(unittest.TestCase):
    STATE = {"stage": "new", "conversation": {"intent": "question", "sentiment": 0.7, "messages_count": 3},
             "identity": {"region": "MX", "name": "SECRET NAME", "username": "secret_user"}, "conversation_last": "x"}

    def test_pre_action_features_with_provenance(self):
        v = build_features("pd_1", 5, T0, self.STATE)
        self.assertEqual(v.feature_version, FEATURE_VERSION)
        self.assertEqual([f.feature_id for f in v.features], list(FEATURE_IDS))
        self.assertEqual((v.get("stage"), v.get("intent"), v.get("sentiment"), v.get("region"), v.get("messages")),
                         ("START", "question", 0.7, "MX", 3))
        for f in v.features:
            self.assertEqual((f.source, f.observed_at), ("client_state_snapshot", T0))
            self.assertLessEqual(f.observed_at, v.as_of)
        blob = str(v.to_dict())
        for secret in ("SECRET", "secret_user"):
            self.assertNotIn(secret, blob)                                   # PII minimised

    def test_unknown_values_are_absent_not_guessed(self):
        v = build_features("pd_1", 5, T0, {"stage": "qualified", "intent": "unknown", "sentiment": 0.5, "region": "?", "messages_count": 0})
        self.assertEqual([f.feature_id for f in v.features], ["messages"])   # unmapped stage, no-text intent/sentiment, '?' region
        self.assertEqual(build_features("pd_1", 5, T0, None).features, ())
        self.assertEqual(build_features("pd_1", 5, T0, "junk").features, ())

    def test_leakage_future_and_foreign_features_rejected(self):
        for fid in ("outcome", "deposit", "future_state", "next_message", "post_action_evidence", "conversion", "shadow_momentum"):
            with self.assertRaises(LeakageError, msg=fid):
                Feature(fid, 1, "client_state_snapshot", T0)
        for src in ("outcomes", "shadow", "postback", "future_client_state", "response_boundary"):
            with self.assertRaises(LeakageError, msg=src):
                Feature("stage", "START", src, T0)
        late = Feature("stage", "START", "client_state_snapshot", T0 + timedelta(seconds=1))
        with self.assertRaises(LeakageError):
            FeatureVector(FEATURE_VERSION, "pd_1", 5, T0, (late,))              # observed after the decision
        ok = Feature("stage", "START", "client_state_snapshot", T0)
        FeatureVector(FEATURE_VERSION, "pd_1", 5, T0, (ok,))                    # equality with decision time is fine
        self.assertEqual(ALLOWED_SOURCES, frozenset({"client_state_snapshot"}))
        self.assertTrue(set(FEATURE_IDS).isdisjoint({"outcome", "deposit", "payment", "conversion", "future"}))

    def test_vector_structure_rules(self):
        a, b = (Feature(i, v, "client_state_snapshot", T0) for i, v in (("stage", "START"), ("intent", "question")))
        with self.assertRaises(ContractError):
            FeatureVector(FEATURE_VERSION, "pd_1", 5, T0, (b, a))             # not canonical order
        with self.assertRaises(ContractError):
            FeatureVector(FEATURE_VERSION, "pd_1", 5, T0, (a, a))
        with self.assertRaises(ContractError):
            FeatureVector("features.v9", "pd_1", 5, T0, (a,))
        for bad in (None, True, [1], {"x": 1}):
            with self.assertRaises(ContractError):
                Feature("stage", bad, "client_state_snapshot", T0)
        with self.assertRaises(ContractError):
            FeatureVector(FEATURE_VERSION, "pd_1", 5, datetime(2026, 1, 1), (a,))
        v = FeatureVector(FEATURE_VERSION, "pd_1", 5, T0, (a, b))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            v.chat_id = 6

    def test_audit_and_determinism(self):
        vs = [build_features(f"pd_{i}", 5, T0, self.STATE) for i in range(3)]
        self.assertEqual(audit_vectors(vs), 0)
        self.assertEqual(build_features("pd_1", 5, T0, self.STATE).fingerprint(), build_features("pd_1", 5, T0, dict(self.STATE)).fingerprint())
        self.assertNotEqual(vs[0].fingerprint(), vs[1].fingerprint())
        forged = object.__new__(FeatureVector)
        for k, val in (("feature_version", FEATURE_VERSION), ("decision_id", "pd_x"), ("chat_id", 5), ("as_of", T0),
                       ("features", (object.__new__(Feature),))):
            object.__setattr__(forged, k, val)
        object.__setattr__(forged.features[0], "feature_id", "outcome")
        for k, val in (("value", 1), ("source", "client_state_snapshot"), ("observed_at", T0)):
            object.__setattr__(forged.features[0], k, val)
        self.assertEqual(audit_vectors([forged]), 1)                            # an independent audit catches a forged vector


if __name__ == "__main__":
    unittest.main()

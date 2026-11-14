import json
import logging

import pytest

from copilot_voice import decision as D
from copilot_voice.adapter import MockVoiceJobDispatcher, canonical_body, sign_body
from copilot_voice.models import VoiceContext, VoiceReason

LONG = ("Entiendo cómo te sientes, no te preocupes. " + "Te lo explico paso a paso, con calma y con detalle. " * 8)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    D.reset_state()
    D.set_dispatcher(MockVoiceJobDispatcher())
    monkeypatch.setenv("COPILOT_VOICE_DECISION_ENABLED", "true")
    monkeypatch.delenv("COPILOT_VOICE_AUTOSEND_ENABLED", raising=False)
    yield
    D.reset_state()


def ctx(**kw):
    base = dict(chat_id=12345, reply_text="ok vale, hablamos luego " * 4, inbound_text="hola")
    base.update(kw)
    return VoiceContext(**base)


def test_client_requested_voice_yes():
    r = D.decide(ctx(inbound_text="mándame un audio porfa"))
    assert r.should_send_voice and r.reason is VoiceReason.CLIENT_REQUESTED_VOICE


def test_short_neutral_no():
    assert not D.decide(ctx(reply_text="Vale, perfecto")).should_send_voice


def test_long_explanatory_yes():
    r = D.decide(ctx(reply_text=LONG[:900]))
    assert r.should_send_voice and r.score >= 0.6


def test_cooldown_no():
    assert not D.decide(ctx(reply_text=LONG[:900], seconds_since_last_voice=60)).should_send_voice


def test_cooldown_from_state():
    assert D.evaluate_and_dispatch(ctx(reply_text=LONG[:900])).should_send_voice
    assert not D.evaluate_and_dispatch(ctx(reply_text=LONG[:900])).should_send_voice


@pytest.mark.parametrize("cid", [None, "", 0, "abc", True])
def test_missing_chat_id_no(cid):
    assert not D.decide(ctx(chat_id=cid, inbound_text="audio")).should_send_voice


def test_disabled_no_side_effects(monkeypatch, caplog):
    monkeypatch.setenv("COPILOT_VOICE_DECISION_ENABLED", "false")
    calls = []
    D.set_dispatcher(type("X", (), {"dispatch": lambda s, j: calls.append(j)})())
    with caplog.at_level(logging.INFO, logger="copilot_voice"):
        assert D.evaluate_and_dispatch(ctx(inbound_text="voice message")) is None
    assert not calls and not caplog.records and not D._last_voice


def test_decision_only_does_not_dispatch():
    calls = []
    D.set_dispatcher(type("X", (), {"dispatch": lambda s, j: calls.append(j)})())
    assert D.evaluate_and_dispatch(ctx(inbound_text="nota de voz")).should_send_voice
    assert calls == []


def test_autosend_dispatches_job(monkeypatch):
    monkeypatch.setenv("COPILOT_VOICE_AUTOSEND_ENABLED", "true")
    calls = []
    D.set_dispatcher(type("X", (), {"dispatch": lambda s, j: calls.append(j)})())
    D.evaluate_and_dispatch(ctx(inbound_text="nota de voz", correlation_id="pd_1"))
    assert calls[0]["correlation_id"] == "pd_1" and calls[0]["reason"] == "client_requested_voice"


def test_dispatcher_failure_is_swallowed(monkeypatch, caplog):
    monkeypatch.setenv("COPILOT_VOICE_AUTOSEND_ENABLED", "true")

    class Boom:
        def dispatch(self, job):
            raise RuntimeError("n8n down")

    D.set_dispatcher(Boom())
    with caplog.at_level(logging.INFO, logger="copilot_voice"):
        r = D.evaluate_and_dispatch(ctx(inbound_text="nota de voz"))      # no exception escapes
        assert r is not None and r.should_send_voice
    assert any("VOICE_DECISION_ERROR" in r.getMessage() for r in caplog.records)


def test_policy_failure_is_swallowed():
    D.set_policy(type("P", (), {"evaluate": lambda s, c: 1 / 0})())
    try:
        assert D.evaluate_and_dispatch(ctx()) is None
    finally:
        from copilot_voice.policy import RulePolicyV1
        D.set_policy(RulePolicyV1())


def test_deterministic():
    a, b = D.decide(ctx(reply_text=LONG[:900])), D.decide(ctx(reply_text=LONG[:900]))
    key = lambda r: (r.should_send_voice, r.score, r.reason, r.priority, r.voice_profile, r.policy_version)
    assert key(a) == key(b)


def test_reason_enum_only():
    allowed = {r.value for r in VoiceReason}
    assert allowed == {"client_requested_voice", "emotional_explanation", "complex_explanation", "persuasion",
                       "objection_handling", "rapport", "natural_voice_preferred", "long_text", "other"}
    for t in ("audio", "hola", LONG[:900], "x" * 5000):
        assert D.decide(ctx(inbound_text=t, reply_text=LONG[:900])).to_job()["reason"] in allowed


def test_correlation_id_preserved_and_generated():
    assert D.decide(ctx(correlation_id="pd_abc")).correlation_id == "pd_abc"
    r = D.decide(ctx())
    assert r.correlation_id == r.decision_id and r.decision_id.startswith("vd_")


def test_event_has_no_text_or_raw_chat_id(caplog):
    with caplog.at_level(logging.INFO, logger="copilot_voice"):
        D.evaluate_and_dispatch(ctx(chat_id=987654321, inbound_text="audio", reply_text="secret body " * 10))
    msg = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("VOICE_DECISION "))
    ev = json.loads(msg.split(" ", 1)[1])
    assert "987654321" not in msg and "secret body" not in msg
    assert ev["text_length"] == 119 and ev["policy_version"] == "voice_policy_v1"
    assert {"ts", "chat_id", "correlation_id", "should_send_voice", "score", "reason"} <= set(ev)


def test_service_regime_no():
    assert not D.decide(ctx(regime="analyst", inbound_text="audio")).should_send_voice
    assert not D.decide(ctx(is_service=True, inbound_text="audio")).should_send_voice


def test_signature_scheme_matches_traffic_os():
    import hashlib, hmac
    body = canonical_body({"a": 1})
    assert sign_body("s", body) == "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()

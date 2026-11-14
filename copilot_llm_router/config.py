"""Configuration from an env MAPPING (injected; nothing is read at import time). Keys are never read: only the presence of the variable is recorded.

  LLM_ROUTER_ENABLED (false)  LLM_FAILOVER_ENABLED (false)  LLM_DRY_RUN (true)  LLM_LIMITS_SHARED_ACROSS_KEYS (unknown|true|false)
  LLM_{PRIMARY,SECONDARY,EMERGENCY}_{PROVIDER,MODEL,KEY,ENABLED,TIMEOUT,MAX_CONCURRENCY}
  LLM_EMERGENCY_ALLOW_DIFFERENT_MODEL (false)
A slot MODEL left empty means "the model the existing selection chose for the request" (same-model by construction)."""
from typing import Mapping

from copilot_llm_router.contracts import ORDER, RouterConfig, Shared, Slot, SlotConfig, Thresholds

LEGACY_KEY_ENV = "LLM_API_KEY"            # the production key variable: PRIMARY falls back to it by NAME only
TRUE = {"1", "true", "yes", "on"}


def _flag(env: Mapping, name: str, default: bool) -> bool:
    v = env.get(name)
    return default if v is None or str(v).strip() == "" else str(v).strip().lower() in TRUE


def _num(env: Mapping, name: str, default, cast):
    try:
        v = cast(env.get(name))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _shared(env: Mapping) -> Shared:
    v = str(env.get("LLM_LIMITS_SHARED_ACROSS_KEYS", "")).strip().lower()
    return {"true": Shared.SHARED, "false": Shared.INDEPENDENT}.get(v, Shared.UNKNOWN)


def load_config(env: Mapping, *, thresholds: Thresholds | None = None) -> RouterConfig:
    slots = []
    for s in ORDER:
        p = f"LLM_{s.value}_"
        key_env = p + "KEY"
        if s is Slot.PRIMARY and not env.get(key_env) and env.get(LEGACY_KEY_ENV):
            key_env = LEGACY_KEY_ENV
        slots.append(SlotConfig(slot=s, provider=(env.get(p + "PROVIDER") or "").strip() or None, model=(env.get(p + "MODEL") or "").strip() or None,
                                key_slot=s.value.lower(), key_env=key_env, key_present=bool(str(env.get(key_env, "")).strip()),
                                enabled=_flag(env, p + "ENABLED", True), timeout_s=_num(env, p + "TIMEOUT", 90.0, float),
                                max_concurrency=_num(env, p + "MAX_CONCURRENCY", 10, int)))
    return RouterConfig(router_enabled=_flag(env, "LLM_ROUTER_ENABLED", False), failover_enabled=_flag(env, "LLM_FAILOVER_ENABLED", False),
                        dry_run=_flag(env, "LLM_DRY_RUN", True), slots=tuple(slots), limits_shared_across_keys=_shared(env),
                        emergency_allow_different_model=_flag(env, "LLM_EMERGENCY_ALLOW_DIFFERENT_MODEL", False),
                        thresholds=thresholds or Thresholds())


def validate(cfg: RouterConfig) -> list[str]:
    """Problems that make SAME_MODEL_FAILOVER invalid. Empty list = compatible."""
    out = []
    p, s, e = cfg.slot(Slot.PRIMARY), cfg.slot(Slot.SECONDARY), cfg.slot(Slot.EMERGENCY)
    if p is None or s is None:
        return ["primary_and_secondary_required"]
    if p.provider != s.provider:
        out.append(f"secondary_provider_differs:{p.provider}!={s.provider}")
    if p.model != s.model:
        out.append(f"secondary_model_differs:{p.model}!={s.model}")       # incompatible for SAME_MODEL_FAILOVER
    if len({c.key_slot for c in cfg.slots}) != len(cfg.slots):
        out.append("key_slots_not_unique")
    if p.key_env == s.key_env:
        out.append("secondary_uses_primary_credential")                    # same credential: not a failover
    if e is not None and e.enabled and not cfg.emergency_allow_different_model and (e.provider, e.model) != (p.provider, p.model):
        out.append("emergency_model_differs")
    return out


def same_model(slot: SlotConfig, request_model: str | None, primary: SlotConfig) -> bool:
    """May `slot` serve a request whose model was chosen by the existing selection?"""
    return slot.model is None or slot.model == request_model

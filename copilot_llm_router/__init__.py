"""LLM execution-slot router (infrastructure only). Question it answers: "which key slot executes this request?" — PRIMARY / SECONDARY / EMERGENCY.
SECONDARY = same provider + same model + a different credential slot. It knows nothing about prompts, psychology, sales, Client State, Uplift,
Policy, Telegram or leads, makes no network calls itself (the provider call is injected) and is NOT wired into production: with
LLM_ROUTER_ENABLED=false (default) the existing path is untouched. Phase 1: architecture + dry-run simulation, zero real keys."""

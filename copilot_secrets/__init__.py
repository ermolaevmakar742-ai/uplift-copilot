"""Encrypted storage and admin management of LLM API keys (PRIMARY / SECONDARY / EMERGENCY). Phase 1: secret management only.
No key is ever stored in plaintext, returned by an API, logged or audited; the master key lives OUTSIDE the database (COPILOT_SECRET_MASTER_KEY);
no network call is made (connection test is a dry-run abstraction). Not connected to the LLM path or the router: the future router reads
`get_secret_for_runtime(slot)`, never .env directly."""

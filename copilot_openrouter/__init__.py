"""Read-only OpenRouter key status (balance / limits / usage) for the admin panel. Backend -> OpenRouter over HTTPS only; the key comes solely from
`copilot_secrets...get_secret_for_runtime(slot)` and never reaches the browser, logs, audit or cache. Not connected to the LLM router or the production LLM path."""

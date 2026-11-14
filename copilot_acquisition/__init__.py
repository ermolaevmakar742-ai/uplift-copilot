"""COPILOT Acquisition Attribution v1: CAMPAIGN -> CLICK -> TELEGRAM HANDOFF -> LEAD, joined read-only to decisions / outcomes / reward.

Reuses the existing `attribution_clicks` table and `users.attribution_*` fields; adds only a campaign registry, a side table keyed by
click_id and a pseudonymous per-lead first/last-touch record. Separate from `copilot_attribution` (outcome -> decision attribution).
Off unless COPILOT_ACQUISITION_ENABLED is true. No IP / fingerprint / phone / email / username is ever stored.
"""

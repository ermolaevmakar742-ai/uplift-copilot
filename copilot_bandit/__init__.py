"""COPILOT research layer: Shadow Store v2 fields + in-process contextual bandit (shadow only).

Nothing here chooses, changes or delays a production reply. The bandit RECOMMENDS hypothetically; the recommendation is
written next to (never over) the production action. No BanditDB / VW, no new service, numpy only.
Does not import copilot_shadow (the shadow hook calls in; no cycles).
"""

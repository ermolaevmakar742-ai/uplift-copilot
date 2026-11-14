"""COPILOT Dialogue Learning Loop v1 (research / shadow ONLY).

critic -> examples -> patterns -> candidate improvements -> offline evaluation. Output is DATA for humans and future retrieval:
nothing here edits a prompt, route, action, model or response policy, and nothing is ever applied automatically.
Flag COPILOT_DIALOGUE_LEARNING_SHADOW_ENABLED (default off). No new DB / service / network call; deterministic rules, no LLM.
"""

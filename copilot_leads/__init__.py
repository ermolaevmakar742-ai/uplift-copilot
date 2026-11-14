"""LEAD JOURNEY: a read-mostly panel view of real leads on the canonical stage axis, plus an append-only HUMAN stage-override log.
Isolated from production: nothing here is imported by the bot's reply path, and the override never changes Client State / users.stage,
Decisions, Outcomes, Uplift or Shadow. Only copilot_leads.api is mounted (admin panel)."""

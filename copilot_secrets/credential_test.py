"""Provider credential test abstraction. Phase 1 is DRY-RUN ONLY: no HTTP call, no provider SDK, no network. A real provider-specific check can later
replace `test_provider_credentials` behind the same signature (it would receive the SecretValue and return one of the same results)."""
from copilot_secrets.contracts import SecretValue, TestResult

DRY_RUN_DETAIL = "CONFIGURATION_ONLY"


def test_provider_credentials(slot: str, provider: str | None, secret: "SecretValue | None", *, dry_run: bool = True) -> tuple[TestResult, str]:
    """-> (result, detail). Never raises, never logs, never returns the value."""
    if secret is None or not provider:
        return TestResult.NOT_CONFIGURED, "no_active_key"
    if not dry_run:
        return TestResult.UNAVAILABLE, "real_provider_test_not_implemented"        # no real calls in this phase
    return TestResult.DRY_RUN, DRY_RUN_DETAIL

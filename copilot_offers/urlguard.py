"""Pure outbound URL guard (NOT wired into any send path in V2.2). The model is never the source of truth for a registration URL:
a URL may be used only if it is EXACTLY the assigned one. No fuzzy matching, no normalisation, no redirect following, no network."""
from enum import Enum

from .validate import valid_url


class UrlCheck(str, Enum):
    VALID = "VALID"          # exactly the assigned URL
    INVALID = "INVALID"      # anything else, including domain-only, lookalikes, other schemes, padded strings
    NO_URL = "NO_URL"        # the model produced no URL: not an error (a link is optional)


def validate_offer_url(candidate_url, assigned_url) -> UrlCheck:
    if candidate_url is None or (isinstance(candidate_url, str) and candidate_url == ""):
        return UrlCheck.NO_URL
    if not isinstance(candidate_url, str) or not valid_url(assigned_url):
        return UrlCheck.INVALID
    return UrlCheck.VALID if candidate_url == assigned_url else UrlCheck.INVALID


def is_acceptable(check: UrlCheck) -> bool:
    """Only VALID or NO_URL may ever reach a user."""
    return check in (UrlCheck.VALID, UrlCheck.NO_URL)

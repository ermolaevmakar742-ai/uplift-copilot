"""Telegram account identity (pseudonymous).

COPILOT runs one Telethon user account today (main.ME_ID). Its stable reference is a one-way pseudonym; the raw id, tokens and the
session are never stored, logged or returned.

    python3 -m copilot_offers.accounts ref <telegram_account_id>     # prints the account_ref (no DB access)
"""
import hashlib
import sys


def account_ref_for(account_id) -> str | None:
    """'acct_<12 hex>' or None for an unusable id (never guesses)."""
    if isinstance(account_id, bool):
        return None
    try:
        v = int(account_id)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return "acct_" + hashlib.sha256(f"ares-offers-account:{v}".encode()).hexdigest()[:12]


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "ref":
        print(account_ref_for(sys.argv[2]) or "invalid account id")
    else:
        print(__doc__)

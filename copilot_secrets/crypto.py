"""Encryption with the library already in the project (cryptography, Fernet = AES-128-CBC + HMAC-SHA256, authenticated). No home-made crypto.
The master key is external (COPILOT_SECRET_MASTER_KEY, a Fernet key; optional COPILOT_SECRET_MASTER_KEY_PREVIOUS, comma separated, decrypt-only for master-key rotation).
Missing / invalid master key => the box is UNAVAILABLE: no encryption, no decryption, NO plaintext fallback, and nothing raises into the application.
The payload binds the ciphertext to (slot, version): a token copied to another slot or version does not decrypt."""
import json
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

MASTER_ENV = "COPILOT_SECRET_MASTER_KEY"
PREVIOUS_ENV = "COPILOT_SECRET_MASTER_KEY_PREVIOUS"
PAYLOAD_VERSION = 1


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


class SecretBox:
    def __init__(self, env: Mapping[str, str]):
        self.state = "missing"
        self._f: MultiFernet | None = None
        raw = str(env.get(MASTER_ENV, "") or "").strip()
        if not raw:
            return
        try:
            keys = [Fernet(raw.encode())]
            for p in str(env.get(PREVIOUS_ENV, "") or "").split(","):
                if p.strip():
                    keys.append(Fernet(p.strip().encode()))
            self._f = MultiFernet(keys)
            self.state = "configured"
        except Exception:
            self.state = "invalid"                       # never echo the offending value

    @property
    def available(self) -> bool:
        return self._f is not None

    def encrypt(self, slot: str, version: int, plaintext: str) -> str:
        if self._f is None:
            raise RuntimeError("master_key_unavailable")
        payload = json.dumps({"p": PAYLOAD_VERSION, "s": slot, "v": version, "k": plaintext}, separators=(",", ":")).encode()
        return self._f.encrypt(payload).decode()

    def decrypt(self, slot: str, version: int, token: str) -> str | None:
        """None on ANY problem (wrong key, tampering, wrong slot / version, bad payload); the reason is deliberately not distinguishable."""
        if self._f is None or not token:
            return None
        try:
            d = json.loads(self._f.decrypt(token.encode()))
            if d.get("p") == PAYLOAD_VERSION and d.get("s") == slot and d.get("v") == version and isinstance(d.get("k"), str):
                return d["k"]
        except (InvalidToken, ValueError, TypeError, KeyError):
            pass
        return None

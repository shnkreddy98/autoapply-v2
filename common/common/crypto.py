"""At-rest encryption for secrets stored in Postgres (Gmail refresh tokens; portal
passwords when they move over). Fernet (AES-128-CBC + HMAC) with a single symmetric
key from the AUTOAPPLY_ENC_KEY env var — the key must never live in the DB, so a
database dump alone cannot decrypt anything.

Generate a key once per deployment:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and put it in .env as AUTOAPPLY_ENC_KEY. Losing the key means re-connecting every
user's grant; leaking it downgrades you to plaintext-at-rest, so treat it like a
root credential (secret manager in prod).
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("AUTOAPPLY_ENC_KEY")
    if not key:
        raise RuntimeError(
            "AUTOAPPLY_ENC_KEY is not set — generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and add it to .env"
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise RuntimeError(f"AUTOAPPLY_ENC_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Output is urlsafe base64 text (fits text columns)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a stored secret. Raises RuntimeError on key mismatch/corruption."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "could not decrypt stored secret — AUTOAPPLY_ENC_KEY differs from the "
            "key that encrypted it (rotated/lost?), or the ciphertext is corrupt"
        ) from exc

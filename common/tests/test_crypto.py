"""Unit tests for common.crypto (Fernet at-rest encryption). No DB needed."""

import pytest
from cryptography.fernet import Fernet

from common import crypto


def test_roundtrip():
    assert crypto.decrypt(crypto.encrypt("refresh-token-1")) == "refresh-token-1"


def test_ciphertext_differs_from_plaintext():
    ct = crypto.encrypt("secret")
    assert "secret" not in ct


def test_missing_key(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_ENC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AUTOAPPLY_ENC_KEY is not set"):
        crypto.encrypt("x")


def test_invalid_key(monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_ENC_KEY", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        crypto.encrypt("x")


def test_wrong_key_decrypt(monkeypatch):
    ct = crypto.encrypt("secret")
    monkeypatch.setenv("AUTOAPPLY_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="could not decrypt"):
        crypto.decrypt(ct)

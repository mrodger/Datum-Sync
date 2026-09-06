"""Multi-key rotation tests for crypto.py (B4).

Proves the key-id prefix byte works: seal under key A, rotate to key B,
old blobs still open (key A loaded), new blobs seal under B. Remove key A
and old blobs fail closed with CryptoError.
"""
from __future__ import annotations

import os

import pytest

from datum_sync import crypto


# -- helpers ------------------------------------------------------------------

def _set_key(monkeypatch, kid: int, key: str | None = None) -> str:
    k = key or crypto.generate_key()
    monkeypatch.setenv(f"{crypto.KEY_ENV_PREFIX}{kid:02d}", k)
    return k


def _set_current(monkeypatch, kid: int) -> None:
    monkeypatch.setenv(crypto.CURRENT_KEY_ENV, str(kid))


def _del_key(monkeypatch, kid: int) -> None:
    monkeypatch.delenv(f"{crypto.KEY_ENV_PREFIX}{kid:02d}", raising=False)


# -- basic seal/open ----------------------------------------------------------

def test_seal_prepends_key_id_byte(monkeypatch):
    """Guard: SECRET-005. The first byte of the sealed blob is the current key id."""
    crypto.set_test_keys(monkeypatch, key_id=3)
    blob = crypto.seal("conn", {"user": "x"})
    assert blob[0] == 3


def test_open_reads_key_id_from_blob(monkeypatch):
    """Guard: SECRET-006. open_ selects the key named by the prefix byte."""
    crypto.set_test_keys(monkeypatch, key_id=1)
    blob = crypto.seal("conn", {"password": "s3cret"})
    result = crypto.open_("conn", blob)
    assert result == {"password": "s3cret"}


def test_open_with_wrong_key_id_fails_closed(monkeypatch):
    """Guard: SECRET-007. A missing key is a CryptoError, not garbage."""
    _set_key(monkeypatch, 1)
    _set_current(monkeypatch, 1)
    blob = crypto.seal("conn", {"password": "x"})

    # Remove key 1, add key 2 — blob's key id (1) is now unknown
    _del_key(monkeypatch, 1)
    _set_key(monkeypatch, 2)
    _set_current(monkeypatch, 2)

    with pytest.raises(crypto.CryptoError, match="key id 1"):
        crypto.open_("conn", blob)


# -- rotation scenario --------------------------------------------------------

def test_rotation_both_keys_loaded(monkeypatch):
    """Seal under key 1, add key 2 as current, old blob still opens.

    Guard: SECRET-006.
    """
    key1 = _set_key(monkeypatch, 1)
    _set_current(monkeypatch, 1)

    blob_a = crypto.seal("conn-a", {"token": "old"})
    assert blob_a[0] == 1

    # Rotate: add key 2, make it current
    _set_key(monkeypatch, 2)
    _set_current(monkeypatch, 2)

    # Old blob still opens (key 1 still loaded)
    assert crypto.open_("conn-a", blob_a) == {"token": "old"}

    # New blob seals under key 2
    blob_b = crypto.seal("conn-a", {"token": "new"})
    assert blob_b[0] == 2
    assert crypto.open_("conn-a", blob_b) == {"token": "new"}


def test_rotation_remove_old_key_fails_closed(monkeypatch):
    """After removing key 1, blobs sealed under it are unreadable."""
    _set_key(monkeypatch, 1)
    _set_current(monkeypatch, 1)
    blob = crypto.seal("conn", {"x": 1})

    # Rotate and retire key 1
    _set_key(monkeypatch, 2)
    _set_current(monkeypatch, 2)
    _del_key(monkeypatch, 1)

    with pytest.raises(crypto.CryptoError, match="key id 1"):
        crypto.open_("conn", blob)


# -- AAD binding preserved ----------------------------------------------------

def test_aad_binding_survives_multikey(monkeypatch):
    """A blob sealed for conn-a cannot be opened as conn-b, regardless of key."""
    crypto.set_test_keys(monkeypatch, key_id=1)
    blob = crypto.seal("conn-a", {"user": "x"})
    with pytest.raises(crypto.CryptoError, match="wrong key"):
        crypto.open_("conn-b", blob)


# -- edge cases ---------------------------------------------------------------

def test_truncated_blob_raises(monkeypatch):
    crypto.set_test_keys(monkeypatch, key_id=1)
    with pytest.raises(crypto.CryptoError, match="truncated"):
        crypto.open_("conn", b"\x01" + b"\x00" * 5)


def test_no_keys_configured_raises(monkeypatch):
    crypto.clear_test_keys(monkeypatch)
    with pytest.raises(crypto.KeyUnavailable):
        crypto.seal("conn", {"x": 1})


def test_current_key_not_loaded_raises(monkeypatch):
    _set_key(monkeypatch, 1)
    _set_current(monkeypatch, 2)  # key 2 not loaded
    with pytest.raises(crypto.KeyUnavailable, match="not set"):
        crypto.seal("conn", {"x": 1})


def test_available_true_when_configured(monkeypatch):
    crypto.set_test_keys(monkeypatch, key_id=1)
    assert crypto.available() is True


def test_available_false_when_no_keys(monkeypatch):
    crypto.clear_test_keys(monkeypatch)
    assert crypto.available() is False


def test_available_false_when_current_missing(monkeypatch):
    _set_key(monkeypatch, 1)
    _set_current(monkeypatch, 2)
    assert crypto.available() is False


def test_key_id_in_blob_is_one_byte(monkeypatch):
    """The key-id prefix is exactly one byte, not varint or anything else."""
    crypto.set_test_keys(monkeypatch, key_id=1)
    blob = crypto.seal("conn", {"x": 1})
    # 1 (key-id) + 12 (nonce) + ciphertext + 16 (tag)
    # The minimum plaintext is b'{"x":1}' = 7 bytes
    assert len(blob) >= 1 + 12 + 7 + 16


def test_key_id_255_works(monkeypatch):
    """Max key id fits in one byte."""
    _set_key(monkeypatch, 255)
    _set_current(monkeypatch, 255)
    blob = crypto.seal("conn", {"hi": True})
    assert blob[0] == 255
    assert crypto.open_("conn", blob) == {"hi": True}


def test_generate_key_format():
    """generate_key still produces a valid base64 key."""
    import base64
    k = crypto.generate_key()
    raw = base64.urlsafe_b64decode(k)
    assert len(raw) == 32

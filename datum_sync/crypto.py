"""Authenticated encryption for connection secrets.

AES-256-GCM. The sealed value is `nonce || ciphertext || tag`, where the tag is
the 16 bytes GCM appends -- that is `AESGCM.encrypt`'s own output format, so
this module only prepends the nonce.

Two choices worth stating.

**The connection name is the associated data.** It is authenticated but not
encrypted, which means a sealed secret cannot be moved between rows: copy
connection A's `secret` column onto connection B and the open fails outright
rather than handing B the credentials of A. Without AAD that swap is invisible
-- the ciphertext decrypts perfectly, because nothing in it says which row it
belonged to. A credential store where rows can be shuffled with a single UPDATE
is not one.

**The key comes from the environment and has no default.** Generating one to a
file under `data/` would make local work frictionless and the encryption
pointless: the key would then live beside the ciphertext, so anyone able to copy
the data directory -- a backup, a stray `scp`, a snapshot -- would have both
halves. Encryption at rest that travels with what it protects is theatre. The
cost is that `datum-sync` refuses to store a secret until it is configured, and
that refusal is the honest failure.

    python -m datum_sync.crypto        print a fresh key for DATUM_SYNC_SECRET_KEY
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "DATUM_SYNC_SECRET_KEY"
KEY_BYTES = 32      # AES-256
NONCE_BYTES = 12    # GCM standard; 96 bits is the size the mode is defined for


class CryptoError(Exception):
    """Sealing or opening failed. Distinct from a missing key."""


class KeyUnavailable(CryptoError):
    """No usable key is configured. Raised at point of use, never at import.

    Import-time would mean the test suite, `--status`, and every command that
    touches no secret all demanding deployment configuration. The check belongs
    to storing credentials, not to loading the module.
    """


def generate_key() -> str:
    """A fresh key, in the form the environment variable expects."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def _load_key() -> bytes:
    raw = os.getenv(KEY_ENV)
    if not raw:
        raise KeyUnavailable(
            f"{KEY_ENV} is not set, so connection secrets cannot be encrypted "
            f"or read. Generate one with `python -m datum_sync.crypto` and set "
            f"it in the environment. It is deliberately not auto-generated into "
            f"the data directory: a key stored beside the ciphertext it "
            f"protects gives no protection against anyone who can copy the "
            f"directory. Losing this key makes every stored secret "
            f"unrecoverable -- back it up separately."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except (binascii.Error, ValueError) as e:
        raise KeyUnavailable(
            f"{KEY_ENV} is not valid base64: {e}. Expected the output of "
            f"`python -m datum_sync.crypto`."
        ) from e
    if len(key) != KEY_BYTES:
        raise KeyUnavailable(
            f"{KEY_ENV} decodes to {len(key)} bytes; AES-256 needs {KEY_BYTES}. "
            f"Expected the output of `python -m datum_sync.crypto`."
        )
    return key


def available() -> bool:
    """True if a usable key is configured. For health checks and the UI.

    The UI needs to explain why the secret fields are refused *before* the user
    fills them in; a 500 on save is a worse way to learn it.
    """
    try:
        _load_key()
        return True
    except KeyUnavailable:
        return False


def _aad(name: str) -> bytes:
    """The associated data a sealed value is bound to.

    Named rather than written out at both call sites: seal and open must agree
    exactly, and two literals that must agree are two literals that can drift.
    """
    return name.encode()


def seal(name: str, payload: dict[str, Any]) -> bytes:
    """Encrypt `payload` bound to connection `name`."""
    key = _load_key()
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    return nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(name))


def open_(name: str, blob: bytes) -> dict[str, Any]:
    """Decrypt a blob sealed for connection `name`.

    Raises CryptoError if the key is wrong, the blob was tampered with, or it
    was sealed for a different connection -- GCM does not distinguish between
    those, and neither should the message: telling a caller *which* of the three
    it was is telling them something about the key.
    """
    key = _load_key()
    if len(blob) <= NONCE_BYTES:
        raise CryptoError(f"sealed value for {name!r} is truncated")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ct, _aad(name))
    except InvalidTag as e:
        raise CryptoError(
            f"cannot open the secret for connection {name!r}: wrong key, "
            f"altered ciphertext, or a value sealed for a different connection"
        ) from e
    return json.loads(plaintext)


if __name__ == "__main__":  # pragma: no cover
    print(f"{KEY_ENV}={generate_key()}")

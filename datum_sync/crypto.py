"""Authenticated encryption for connection secrets.

AES-256-GCM with a key-id prefix byte for rotation support. The sealed value
is ``key_id(1) || nonce(12) || ciphertext || tag(16)``.

**Multiple keys.** Keys are loaded from numbered environment variables:
``DATUM_SYNC_SECRET_KEY_01``, ``DATUM_SYNC_SECRET_KEY_02``, etc.
``DATUM_SYNC_SECRET_KEY_CURRENT`` names the id used for sealing. Old keys
remain loadable so existing blobs can be opened during a rotation window.
When the old key is no longer needed, remove its env var and the blobs it
sealed become unreadable -- that is the point of removal.

**The connection name is the associated data.** It is authenticated but not
encrypted, which means a sealed secret cannot be moved between rows: copy
connection A's ``secret`` column onto connection B and the open fails outright
rather than handing B the credentials of A. Without AAD that swap is invisible
-- the ciphertext decrypts perfectly, because nothing in it says which row it
belonged to. A credential store where rows can be shuffled with a single UPDATE
is not one.

**The key comes from the environment and has no default.** Generating one to a
file under ``data/`` would make local work frictionless and the encryption
pointless: the key would then live beside the ciphertext, so anyone able to
copy the data directory -- a backup, a stray ``scp``, a snapshot -- would have
both halves. Encryption at rest that travels with what it protects is theatre.
The cost is that ``datum-sync`` refuses to store a secret until it is
configured, and that refusal is the honest failure.

    python -m datum_sync.crypto        print a fresh key
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV_PREFIX = "DATUM_SYNC_SECRET_KEY_"
CURRENT_KEY_ENV = "DATUM_SYNC_SECRET_KEY_CURRENT"
# Kept for error messages that name the configuration surface.
KEY_ENV = "DATUM_SYNC_SECRET_KEY_*"
KEY_BYTES = 32      # AES-256
NONCE_BYTES = 12    # GCM standard; 96 bits is the size the mode is defined for
_KEY_ID_BYTES = 1   # prefix byte in sealed blob
MAX_KEY_ID = 255    # one byte


class CryptoError(Exception):
    """Sealing or opening failed. Distinct from a missing key."""


class KeyUnavailable(CryptoError):
    """No usable key is configured. Raised at point of use, never at import.

    Import-time would mean the test suite, ``--status``, and every command that
    touches no secret all demanding deployment configuration. The check belongs
    to storing credentials, not to loading the module.
    """


def generate_key() -> str:
    """A fresh key, in the form the environment variable expects."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


_KEY_SUFFIX_RE = re.compile(r"^(\d{1,3})$")


def _load_keys() -> dict[int, bytes]:
    """Load all numbered keys from the environment.

    Scans for ``DATUM_SYNC_SECRET_KEY_01``, ``_02``, etc. Returns a dict
    mapping integer key id to raw 32-byte key material.
    """
    keys: dict[int, bytes] = {}
    for name, val in os.environ.items():
        if not name.startswith(KEY_ENV_PREFIX):
            continue
        suffix = name[len(KEY_ENV_PREFIX):]
        if suffix == "CURRENT":
            continue
        m = _KEY_SUFFIX_RE.match(suffix)
        if not m:
            continue
        key_id = int(m.group(1))
        if key_id > MAX_KEY_ID:
            raise KeyUnavailable(
                f"{name}: key id {key_id} exceeds {MAX_KEY_ID} (must fit in "
                f"one byte)."
            )
        try:
            raw = base64.urlsafe_b64decode(val)
        except (binascii.Error, ValueError) as e:
            raise KeyUnavailable(
                f"{name} is not valid base64: {e}. Expected the output of "
                f"`python -m datum_sync.crypto`."
            ) from e
        if len(raw) != KEY_BYTES:
            raise KeyUnavailable(
                f"{name} decodes to {len(raw)} bytes; AES-256 needs "
                f"{KEY_BYTES}. Expected the output of "
                f"`python -m datum_sync.crypto`."
            )
        keys[key_id] = raw
    if not keys:
        raise KeyUnavailable(
            f"No secret keys configured. Set {KEY_ENV_PREFIX}01 (and "
            f"optionally more) in the environment. Generate one with "
            f"`python -m datum_sync.crypto`. Keys are deliberately not "
            f"auto-generated into the data directory: a key stored beside the "
            f"ciphertext it protects gives no protection against anyone who "
            f"can copy the directory. Losing a key makes every secret it "
            f"sealed unrecoverable -- back it up separately."
        )
    return keys


def _current_key_id() -> int:
    """The key id to seal new blobs with."""
    raw = os.getenv(CURRENT_KEY_ENV)
    if not raw:
        raise KeyUnavailable(
            f"{CURRENT_KEY_ENV} is not set. It must name the id of the key "
            f"to use for sealing (e.g. '1' if the key is in "
            f"{KEY_ENV_PREFIX}01)."
        )
    try:
        kid = int(raw)
    except ValueError:
        raise KeyUnavailable(
            f"{CURRENT_KEY_ENV}={raw!r} is not an integer."
        )
    if kid < 0 or kid > MAX_KEY_ID:
        raise KeyUnavailable(
            f"{CURRENT_KEY_ENV}={kid} is out of range (0..{MAX_KEY_ID})."
        )
    return kid


def available() -> bool:
    """True if a usable key is configured. For health checks and the UI.

    The UI needs to explain why the secret fields are refused *before* the user
    fills them in; a 500 on save is a worse way to learn it.
    """
    try:
        keys = _load_keys()
        kid = _current_key_id()
        return kid in keys
    except KeyUnavailable:
        return False


def _aad(name: str) -> bytes:
    """The associated data a sealed value is bound to.

    Named rather than written out at both call sites: seal and open must agree
    exactly, and two literals that must agree are two literals that can drift.
    """
    return name.encode()


def seal(name: str, payload: dict[str, Any]) -> bytes:
    """Encrypt ``payload`` bound to connection ``name``.

    The sealed blob is prefixed with the current key id so ``open_`` knows
    which key to use for decryption.
    """
    keys = _load_keys()
    kid = _current_key_id()
    if kid not in keys:
        raise KeyUnavailable(
            f"{CURRENT_KEY_ENV}={kid} but {KEY_ENV_PREFIX}{kid:02d} is not "
            f"set. The current key must be loadable."
        )
    key = keys[kid]
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    return bytes([kid]) + nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(name))


def open_(name: str, blob: bytes) -> dict[str, Any]:
    """Decrypt a blob sealed for connection ``name``.

    Reads the first byte as a key id and selects the matching key. Raises
    ``CryptoError`` if the key id is unknown, the key is wrong, the blob was
    tampered with, or it was sealed for a different connection -- GCM does not
    distinguish between the last three, and neither should the message.
    """
    if len(blob) <= _KEY_ID_BYTES + NONCE_BYTES:
        raise CryptoError(f"sealed value for {name!r} is truncated")
    kid = blob[0]
    keys = _load_keys()
    if kid not in keys:
        raise CryptoError(
            f"sealed value for {name!r} uses key id {kid}, but "
            f"{KEY_ENV_PREFIX}{kid:02d} is not in the environment. "
            f"The key may have been removed after rotation."
        )
    nonce = blob[_KEY_ID_BYTES:_KEY_ID_BYTES + NONCE_BYTES]
    ct = blob[_KEY_ID_BYTES + NONCE_BYTES:]
    try:
        plaintext = AESGCM(keys[kid]).decrypt(nonce, ct, _aad(name))
    except InvalidTag as e:
        raise CryptoError(
            f"cannot open the secret for connection {name!r}: wrong key, "
            f"altered ciphertext, or a value sealed for a different connection"
        ) from e
    return json.loads(plaintext)


def set_test_keys(monkeypatch, *, key_id: int = 1, key: str | None = None) -> str:
    """Set up a single throwaway key for tests. Returns the key string.

    Centralises the env var layout so test files don't encode it. For
    multi-key rotation tests, set the vars directly.
    """
    k = key or generate_key()
    monkeypatch.setenv(f"{KEY_ENV_PREFIX}{key_id:02d}", k)
    monkeypatch.setenv(CURRENT_KEY_ENV, str(key_id))
    return k


def clear_test_keys(monkeypatch) -> None:
    """Remove all secret key env vars. For testing the 'no key' path."""
    for name in list(os.environ):
        if name.startswith(KEY_ENV_PREFIX) or name == CURRENT_KEY_ENV:
            monkeypatch.delenv(name, raising=False)


if __name__ == "__main__":  # pragma: no cover
    print(generate_key())

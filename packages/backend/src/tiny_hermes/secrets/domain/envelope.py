"""Envelope encryption for a Secret: a DEK wraps the value, a KEK wraps the DEK.

The KEK never touches the plaintext. Rotating the KEK rewraps DEKs; it does not
re-encrypt every payload. A dump of the `secrets` table without the matching
KEK is ciphertext.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_LENGTH = 32
NONCE_LENGTH = 12


class InvalidKek(Exception):
    """The configured KEK is missing, not base64, or not 32 bytes."""


class UnwrapFailed(Exception):
    """The KEK, key id, or ciphertext does not match this envelope."""


@dataclass(frozen=True)
class Envelope:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    wrap_nonce: bytes
    key_id: str


def decode_kek(value: str) -> bytes:
    try:
        kek = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise InvalidKek("TINY_HERMES_KEK must be standard base64") from error
    if len(kek) != KEK_LENGTH:
        raise InvalidKek("TINY_HERMES_KEK must decode to 32 bytes")
    return kek


def optional_kek(value: str) -> bytes | None:
    """Worker boot: an empty or invalid KEK is None, and unwrap fails per call."""
    if not value:
        return None
    try:
        return decode_kek(value)
    except InvalidKek:
        return None


def seal(plaintext: bytes, kek: bytes, key_id: str) -> Envelope:
    if len(kek) != KEK_LENGTH:
        raise InvalidKek("a KEK is 32 bytes")
    dek = token_bytes(KEK_LENGTH)
    nonce = token_bytes(NONCE_LENGTH)
    wrap_nonce = token_bytes(NONCE_LENGTH)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, None)
    wrapped_dek = AESGCM(kek).encrypt(wrap_nonce, dek, key_id.encode("utf-8"))
    return Envelope(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_dek=wrapped_dek,
        wrap_nonce=wrap_nonce,
        key_id=key_id,
    )


def unseal(envelope: Envelope, kek: bytes) -> bytes:
    if len(kek) != KEK_LENGTH:
        raise InvalidKek("a KEK is 32 bytes")
    try:
        dek = AESGCM(kek).decrypt(
            envelope.wrap_nonce, envelope.wrapped_dek, envelope.key_id.encode("utf-8")
        )
        return AESGCM(dek).decrypt(envelope.nonce, envelope.ciphertext, None)
    except InvalidTag as error:
        raise UnwrapFailed from error


def rewrap(envelope: Envelope, previous_kek: bytes, new_kek: bytes, new_key_id: str) -> Envelope:
    """Wrap the same DEK under a new KEK. The payload ciphertext does not move."""
    if len(previous_kek) != KEK_LENGTH or len(new_kek) != KEK_LENGTH:
        raise InvalidKek("a KEK is 32 bytes")
    try:
        dek = AESGCM(previous_kek).decrypt(
            envelope.wrap_nonce, envelope.wrapped_dek, envelope.key_id.encode("utf-8")
        )
    except InvalidTag as error:
        raise UnwrapFailed from error
    wrap_nonce = token_bytes(NONCE_LENGTH)
    wrapped_dek = AESGCM(new_kek).encrypt(wrap_nonce, dek, new_key_id.encode("utf-8"))
    return Envelope(
        ciphertext=envelope.ciphertext,
        nonce=envelope.nonce,
        wrapped_dek=wrapped_dek,
        wrap_nonce=wrap_nonce,
        key_id=new_key_id,
    )

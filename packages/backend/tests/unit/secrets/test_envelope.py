from __future__ import annotations

import pytest
from tiny_hermes.secrets.domain.envelope import (
    Envelope,
    InvalidKek,
    UnwrapFailed,
    decode_kek,
    rewrap,
    seal,
    unseal,
)


def test_a_sealed_secret_round_trips() -> None:
    kek = decode_kek("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    sealed = seal(b"model-key-value", kek, "v1")
    assert sealed.key_id == "v1"
    assert sealed.ciphertext != b"model-key-value"
    assert unseal(sealed, kek) == b"model-key-value"


def test_the_wrong_kek_does_not_yield_plaintext() -> None:
    kek = decode_kek("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    other = decode_kek("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=")
    sealed = seal(b"model-key-value", kek, "v1")
    with pytest.raises(UnwrapFailed):
        unseal(sealed, other)


def test_a_key_id_mismatch_does_not_yield_plaintext() -> None:
    kek = decode_kek("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    sealed = seal(b"model-key-value", kek, "v1")
    swapped = Envelope(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_dek=sealed.wrapped_dek,
        wrap_nonce=sealed.wrap_nonce,
        key_id="v2",
    )
    with pytest.raises(UnwrapFailed):
        unseal(swapped, kek)


def test_a_short_kek_is_refused() -> None:
    with pytest.raises(InvalidKek):
        decode_kek("c2hvcnQ=")


def test_rewrap_keeps_the_plaintext_under_a_new_kek() -> None:
    previous = decode_kek("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    current = decode_kek("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=")
    sealed = seal(b"model-key-value", previous, "v1")
    rotated = rewrap(sealed, previous, current, "v2")
    assert rotated.ciphertext == sealed.ciphertext
    assert rotated.key_id == "v2"
    assert unseal(rotated, current) == b"model-key-value"
    with pytest.raises(UnwrapFailed):
        unseal(rotated, previous)

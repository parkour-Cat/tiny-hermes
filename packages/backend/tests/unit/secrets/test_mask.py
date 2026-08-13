from tiny_hermes.secrets.domain.mask import mask_plaintext


def test_mask_keeps_the_first_and_last_two_characters() -> None:
    assert mask_plaintext("abcdefgh") == "ab••••gh"


def test_a_short_value_is_still_masked() -> None:
    assert mask_plaintext("ab") == "••"
    assert mask_plaintext("abcd") == "a••d"
    assert mask_plaintext("x") == "•"


def test_the_mask_is_not_the_plaintext() -> None:
    assert mask_plaintext("secret-value") != "secret-value"

"""A reversible-looking hint that is not reversible.

Computed once from the plaintext at create, then stored. Listing returns this
string; nothing later can recover the value from it.
"""


def mask_plaintext(plaintext: str) -> str:
    if len(plaintext) <= 2:
        return "•" * len(plaintext)
    if len(plaintext) <= 4:
        return plaintext[0] + "•" * (len(plaintext) - 2) + plaintext[-1]
    return plaintext[:2] + "•" * (len(plaintext) - 4) + plaintext[-2:]

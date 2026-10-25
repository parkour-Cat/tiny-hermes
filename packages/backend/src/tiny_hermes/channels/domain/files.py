"""Names for files a sender attaches (§1.27, v2.27)."""

import re

#: Long enough for any name a person types; short of every filesystem's limit.
MAX_NAME = 120

_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def inbox_name(given: str) -> str:
    """The sender's file name, as a single safe name under `inbox/`.

    The sender chooses it, so it must not choose a directory: only the last
    path segment is kept, control characters become `_`, leading dots go, and
    an empty result becomes `file`. Long names keep their extension.
    """
    leaf = re.split(r"[\\/]", given)[-1]
    leaf = _UNSAFE.sub("_", leaf).lstrip(".").strip()
    if not leaf:
        return "file"
    if len(leaf) > MAX_NAME:
        stem, dot, extension = leaf.rpartition(".")
        if dot and 0 < len(extension) <= 10:
            return stem[: MAX_NAME - len(extension) - 1] + "." + extension
        return leaf[:MAX_NAME]
    return leaf

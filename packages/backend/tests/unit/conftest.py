import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Entrypoints configure logging on the root logger, and a test that calls
    one would leave its handler behind, writing to a capture stream that has
    since closed. Every unit test gets the root logger back as it found it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)

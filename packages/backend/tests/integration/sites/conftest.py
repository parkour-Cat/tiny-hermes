import pytest
from tiny_hermes.shared.config import Settings

SITE_PUBLIC_URL = "http://127.0.0.1:8090"
CONSOLE_URL = "http://127.0.0.1:3000"
SITE_SECRET = "a-site-signing-secret-of-32-chars!"  # noqa: S105 - a test value


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The integration settings with site publishing switched on."""
    return settings.model_copy(
        update={
            "site_public_url": SITE_PUBLIC_URL,
            "site_signing_secret": SITE_SECRET,
            "console_base_url": CONSOLE_URL,
        }
    )

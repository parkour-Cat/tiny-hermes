"""`tiny-hermes-site`: the site origin's process (§1.26, v2.26)."""

import uvicorn

from tiny_hermes.shared.config import get_settings
from tiny_hermes.shared.logging import configure_logging
from tiny_hermes.sites.server import create_site_app


def main() -> None:
    configure_logging()
    uvicorn.run(
        create_site_app(get_settings()),
        host="0.0.0.0",  # noqa: S104 - container port must accept traffic outside itself
        port=8090,
    )

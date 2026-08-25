"""The console's proxy must not cache the API's address at startup.

nginx resolves a literal hostname in `proxy_pass` **once, when it starts**,
and keeps that address for the life of the process. Recreate the `api`
container — every deploy does — and it comes back on a new IP while nginx
keeps sending traffic to the old one.

The symptom is the worst kind. The console's home page still loads, because
nginx is serving static files itself; every `/api/` call returns 502. From
the outside the platform looks up and the backend looks dead. It cost a
real Feishu delivery: the webhook got 502, so the message never arrived —
no row in `channel_events`, nothing in the API log, nothing anywhere saying
a request had been made.

Asserted as a ban rather than by starting containers, for the reason
`test_client_ban.py` gives about its own rule: the failure is silent and
somebody will eventually "simplify" the variable back into a literal. This
test is what makes that show up as a red build instead of as a line in a
diff.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
CONFIG = ROOT / "apps" / "web" / "nginx.conf"

#: `proxy_pass` targets that name a host directly. nginx resolves these at
#: startup and never again; only a target containing a variable is resolved
#: per request.
LITERAL_UPSTREAM = re.compile(r"proxy_pass\s+https?://(?!\$)[^;]*;")


def _config() -> str:
    """The directives, with comments stripped.

    Not cosmetic: the comments in that file explain the very trap these
    tests check for, and they quote `proxy_pass http://api:8000;` to do it.
    Reading the raw text made the first version of this test match its own
    documentation and fail against a correct config.
    """
    return "\n".join(
        line for line in CONFIG.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_config_is_where_this_test_thinks_it_is() -> None:
    """Path-based tests rot silently — a moved file makes every assertion
    below vacuously true."""
    assert CONFIG.is_file()


def test_no_proxy_pass_names_its_upstream_literally() -> None:
    found = LITERAL_UPSTREAM.findall(_config())

    assert found == [], (
        "these resolve once at startup and cache the address for the life of "
        f"the process: {found}. Use a variable so nginx resolves per request."
    )


def test_a_resolver_is_configured() -> None:
    """A variable in `proxy_pass` makes nginx resolve per request — and with
    no `resolver` it cannot resolve at all, so every proxied request fails.
    The two changes are one change; splitting them turns a stale address
    into a total outage."""
    assert re.search(r"^\s*resolver\s+\S+", _config(), re.MULTILINE)


@pytest.mark.parametrize("path", ["/api/", "/health/"])
def test_every_proxied_location_forwards_the_original_uri(path: str) -> None:
    """The trap that comes with the fix.

    `proxy_pass http://api:8000;` forwards the request URI on its own. A
    variable form does **not** — `proxy_pass $upstream;` sends `/` and drops
    the path, so every route 404s while the config looks correct. The URI
    has to be appended explicitly.
    """
    block = re.search(
        rf"location {re.escape(path)}\s*\{{(.*?)\n    \}}",
        _config(),
        re.DOTALL,
    )
    assert block is not None, f"no location block for {path}"
    target = re.search(r"proxy_pass\s+([^;]+);", block.group(1))
    assert target is not None, f"{path} does not proxy anywhere"
    assert "$request_uri" in target.group(1), (
        f"{path} would forward a bare / and drop the path: {target.group(1)}"
    )

"""§23.16 (v2.19): the per-address half of login throttling lives in the console's nginx.

The API sees every console request as coming from nginx, so it can only count
per account. nginx sees the browser's address.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
NGINX = ROOT / "apps" / "web" / "nginx.conf"


def test_login_is_limited_per_client_address() -> None:
    conf = NGINX.read_text(encoding="utf-8")

    zone = re.search(r"limit_req_zone\s+\$binary_remote_addr\s+zone=(\w+):\S+\s+rate=(\S+);", conf)
    assert zone, "no per-address zone"
    login = re.search(r"location\s+=\s+/api/v1/auth/sessions\s*\{(?P<body>[^}]*)\}", conf)
    assert login, "no location for the login endpoint"
    assert f"limit_req zone={zone.group(1)}" in login.group("body")
    assert "limit_req_status 429;" in login.group("body")
    # Still reaches the API: a limit that also stopped the request from being
    # proxied would be a login page that never logs anyone in.
    assert "proxy_pass" in login.group("body")

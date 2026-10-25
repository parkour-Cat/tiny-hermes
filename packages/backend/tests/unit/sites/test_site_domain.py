"""What a published site may contain, and how its links and passes are signed (§1.26, v2.26)."""

from datetime import UTC, datetime, timedelta

import pytest
from tiny_hermes.session_workspace.domain.models import EntryType, WorkspaceEntry
from tiny_hermes.sites.domain.site import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SiteRefused,
    media_type,
    new_token,
    same_host,
    same_origin,
    security_headers,
    select_site,
    sign,
    token_digest,
    verify,
)

SECRET = "a-site-signing-secret-of-32-chars!"  # noqa: S105 - a test value
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _file(path: str, size: int = 10) -> WorkspaceEntry:
    return WorkspaceEntry(
        path=path, entry_type=EntryType.FILE, mode=0o644, size=size, sha256="a" * 64
    )


def _dir(path: str) -> WorkspaceEntry:
    return WorkspaceEntry(
        path=path, entry_type=EntryType.DIRECTORY, mode=0o755, size=0, sha256=None
    )


def test_a_site_is_the_files_under_its_root_keyed_by_their_path_inside_it() -> None:
    site = select_site(
        [_dir("site"), _file("site/index.html"), _file("site/js/app.js"), _file("notes.txt")],
        "site",
    )

    assert sorted(site) == ["index.html", "js/app.js"]
    assert site["js/app.js"].path == "site/js/app.js"


@pytest.mark.parametrize("root", ["site/", "./site", "/site"])
def test_a_root_is_read_relative_to_the_data_mount(root: str) -> None:
    assert sorted(select_site([_file("site/index.html")], root)) == ["index.html"]


def test_the_workspace_root_itself_can_be_the_site() -> None:
    site = select_site([_file("index.html"), _file("a.css")], ".")
    assert sorted(site) == ["a.css", "index.html"]


@pytest.mark.parametrize("root", ["../etc", "site/../../x", "a\\b"])
def test_a_root_that_climbs_out_is_refused(root: str) -> None:
    with pytest.raises(SiteRefused) as refused:
        select_site([_file("site/index.html")], root)
    assert refused.value.reason == "bad_path"


def test_a_directory_without_index_html_is_refused() -> None:
    with pytest.raises(SiteRefused) as refused:
        select_site([_file("site/main.html")], "site")
    assert refused.value.reason == "no_index"


def test_a_missing_directory_is_refused_as_missing() -> None:
    with pytest.raises(SiteRefused) as refused:
        select_site([_file("other/index.html")], "site")
    assert refused.value.reason == "not_found"


def test_files_outside_the_allowlist_are_named_not_skipped() -> None:
    """Skipped, the site would be missing something and nobody would know why."""
    with pytest.raises(SiteRefused) as refused:
        select_site(
            [_file("site/index.html"), _file("site/run.sh"), _file("site/db.sqlite")], "site"
        )
    assert refused.value.reason == "file_type"
    assert "run.sh" in refused.value.detail and "db.sqlite" in refused.value.detail


def test_too_many_files_is_refused() -> None:
    entries = [_file("site/index.html")] + [_file(f"site/{i}.css") for i in range(MAX_FILES)]
    with pytest.raises(SiteRefused) as refused:
        select_site(entries, "site")
    assert refused.value.reason == "too_many_files"


def test_too_many_bytes_is_refused() -> None:
    with pytest.raises(SiteRefused) as refused:
        select_site([_file("site/index.html", size=MAX_TOTAL_BYTES + 1)], "site")
    assert refused.value.reason == "too_large"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("index.html", "text/html; charset=utf-8"),
        ("a/b.JS", "text/javascript; charset=utf-8"),
        ("x.svg", "image/svg+xml"),
        ("data.json", "application/json"),
        ("f.woff2", "font/woff2"),
    ],
)
def test_the_media_type_comes_from_the_extension(path: str, expected: str) -> None:
    assert media_type(path) == expected


def test_a_token_is_long_random_and_stored_only_as_its_digest() -> None:
    first, second = new_token(), new_token()
    assert first != second and len(first) >= 43
    assert token_digest(first) == token_digest(first) != first
    assert len(token_digest(first)) == 64


def test_a_signed_value_verifies_until_it_expires() -> None:
    value = sign(SECRET, "ticket", {"p": "pub", "u": "user"}, NOW + timedelta(seconds=60))

    assert verify(SECRET, "ticket", value, NOW) == {"p": "pub", "u": "user"}
    assert verify(SECRET, "ticket", value, NOW + timedelta(seconds=61)) is None


def test_a_signed_value_is_bound_to_its_purpose_and_secret() -> None:
    """A twelve-hour pass must not open the one-minute ticket door, nor the reverse."""
    value = sign(SECRET, "pass", {"p": "pub"}, NOW + timedelta(hours=12))

    assert verify(SECRET, "ticket", value, NOW) is None
    assert verify(SECRET + "x", "pass", value, NOW) is None


@pytest.mark.parametrize("tampered", ["", "x", "abc.def", "...."])
def test_a_malformed_value_is_simply_not_valid(tampered: str) -> None:
    assert verify(SECRET, "pass", tampered, NOW) is None


def test_a_changed_payload_fails_its_signature() -> None:
    value = sign(SECRET, "pass", {"p": "pub"}, NOW + timedelta(hours=1))
    body, mac = value.rsplit(".", 1)
    forged = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + mac

    assert verify(SECRET, "pass", forged, NOW) is None


def test_the_headers_keep_a_site_to_its_own_origin() -> None:
    headers = security_headers()

    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "private, no-cache"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8090", False),
        ("https://hermes.example.com", "https://sites.example.com", False),
        ("https://hermes.example.com", "https://hermes.example.com/s", True),
        ("http://localhost", "http://localhost:80", True),
        ("https://a.example.com", "https://a.example.com:443/x", True),
    ],
)
def test_two_urls_share_an_origin_when_scheme_host_and_port_agree(
    a: str, b: str, same: bool
) -> None:
    assert same_origin(a, b) is same


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8090", True),
        ("http://localhost:8090", "http://127.0.0.1:3000", False),
        ("https://hermes.example.com", "https://sites.example.com", False),
        ("https://Hermes.example.com", "http://hermes.example.com:8443", True),
    ],
)
def test_two_urls_share_a_host_whatever_their_ports(a: str, b: str, same: bool) -> None:
    """What cookies go by: a host, never a port."""
    assert same_host(a, b) is same

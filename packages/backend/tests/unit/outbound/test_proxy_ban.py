"""No code path reaches the network without passing the boundary.

`test_client_ban.py` stops a raw HTTP client being built outside the outbound
module. That was the whole rule while the outbound module *was* the boundary.
Now the boundary is a separate process, and the rule needs two more clauses:

- the one client this platform builds must be pointed at the proxy, and
- nothing outside the two processes whose job is to be an endpoint may open a
  TCP connection of its own.

Both are read out of the source rather than exercised, because what they forbid
is a line somebody adds — not a behaviour a running test could provoke. A ban
that only fails when the bad path is taken is a ban that ships.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
SOURCE = ROOT / "packages" / "backend" / "src" / "tiny_hermes"

#: The client itself, and the only file allowed to build one.
CLIENT = SOURCE / "outbound" / "client.py"

#: Modules allowed to open a socket directly, because being one end of a
#: connection is what they are for: the proxy (it *is* the way out) and the
#: Controller transport (a unix socket to a process on this host, which never
#: leaves it).
ALLOWED_TO_CONNECT = {
    SOURCE / "egress",
    SOURCE / "sandbox" / "transport",
}

#: Ways to open a connection that no lint rule covers. `socket.socket` and the
#: HTTP libraries are banned by TID251; these are the asyncio spellings, which
#: are perfectly ordinary in a server and are exactly how a direct connection
#: would be written by somebody working around the proxy.
CONNECTING = {
    "open_connection",
    "create_connection",
    "open_unix_connection",
}


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _name_of(call: ast.Call) -> str:
    target = call.func
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def test_every_http_client_this_platform_builds_is_pointed_at_the_proxy() -> None:
    """The one construction, and the argument that makes it a boundary.

    Without `proxy=`, this client would open its own connection to the target
    and the whole stage would be a configuration file nobody reads.
    """
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    built = [
        call
        for call in _calls(tree)
        if _name_of(call) in {"AsyncClient", "Client"}
    ]

    assert built, "the outbound client no longer builds an HTTP client at all"
    for call in built:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "proxy" in keywords, (
            f"line {call.lineno}: an HTTP client built without proxy= would "
            "connect directly, which is what this stage exists to prevent"
        )
        assert "trust_env" in keywords, (
            f"line {call.lineno}: without trust_env=False the environment's "
            "own proxy setting decides where this platform's traffic goes"
        )


@pytest.mark.parametrize(
    "module",
    sorted(
        path
        for path in SOURCE.rglob("*.py")
        if not any(path.is_relative_to(allowed) for allowed in ALLOWED_TO_CONNECT)
    ),
    ids=lambda path: str(path.relative_to(SOURCE)),
)
def test_nothing_else_opens_a_connection_of_its_own(module: Path) -> None:
    """asyncio's spellings, which no lint rule covers.

    TID251 bans `socket.socket` and the HTTP libraries; `asyncio.open_connection`
    is neither, is ordinary in a server, and is precisely how somebody would
    write a call that skips the proxy.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found = [call for call in _calls(tree) if _name_of(call) in CONNECTING]

    assert not found, (
        f"{module.relative_to(SOURCE)} opens a connection directly at line "
        f"{found[0].lineno}; outbound traffic goes through the egress proxy"
    )

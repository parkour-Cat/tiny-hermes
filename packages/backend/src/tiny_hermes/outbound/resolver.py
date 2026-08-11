"""Turn a hostname into every address it currently answers with.

Every address, not the first: the policy has to see the whole answer, because a
name that resolves to one permitted and one forbidden address must be refused
rather than raced.

Deliberately synchronous. `getaddrinfo` blocks, so the client runs this in a
thread — but keeping the seam itself a plain function is what lets a test hand
over a resolver that changes its mind between calls, which is the only way to
express DNS rebinding.
"""

import socket
from ipaddress import ip_address

from tiny_hermes.outbound.domain.address_policy import Address


def lookup(host: str, port: int) -> list[Address]:
    try:
        answers = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    found: list[Address] = []
    for entry in answers:
        parsed = ip_address(str(entry[4][0]))
        if parsed not in found:
            found.append(parsed)
    return found

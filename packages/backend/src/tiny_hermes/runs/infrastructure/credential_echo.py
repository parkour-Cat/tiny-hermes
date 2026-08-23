"""Keep a credential from travelling back inside an answer.

Shared by the HTTP tool sender and the MCP gateway. They have no code in
common, inject the same `Authorization` header, and return the far end's
bytes just as directly — so a scrubber living in one of them would be half a
door, and two copies would be one copy nobody updated.
"""

def without_credential(body: str, token: str | None) -> str:
    """§23 assertion 7, at the one place a secret can come back.

    Secrets are resolved for a single call and injected into a header; they
    never enter the model's context. That structural guarantee is stronger
    than scrubbing and holds everywhere except here — `body` becomes the
    tool result the model reads, so a far end that reflects the
    `Authorization` header it was sent hands the credential to the model,
    into the RunEvent, and within reach of a memory proposal.

    Echoing is the far end's doing, not this platform's, and the far end
    already holds the secret — nothing is disclosed to *it*. What changes is
    where the secret then lives: session content a developer may read under
    audit, and memory that outlives the Run. Those have different access
    rules than "held by the outbound layer for the length of one call", and
    moving a credential between them silently is what this prevents.

    Replaced rather than refused: the answer is still the answer, and a Run
    that failed because its API was chatty would be a worse outcome than one
    that read a redacted field.
    """
    if not token:
        return body
    return body.replace(token, "[redacted credential]")

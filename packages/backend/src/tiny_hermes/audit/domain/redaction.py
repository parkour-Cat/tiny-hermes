"""§2: what a 查看者 (viewer) may see inside one audit row's `context`.

`context` is the only column on `AuditEventRow` that can hold business
data — `workspace_id`, `actor_type`, `actor_id`, `action`, `resource_type`,
`resource_id`, `result` and `request_id` are all identifiers or fixed
vocabulary, safe for anyone who can already see the row at all. Redaction
therefore acts on `context` alone, never on the rest of the row, and never
on any subject but the viewer — a workspace administrator who cannot read
`context` in full cannot investigate an incident, which is the whole reason
that row exists.

**Whitelist, not blacklist** — plan §8's first decision, restated here
because this is the module a future blacklist would have been added to
instead. A blacklist's failure mode is silent: the next person who puts a
new key into some `append_audit(context={...})` call has no reason to
remember this file exists, and nothing turns red the day they do not. A
whitelist's failure mode is loud in the other direction — a key nobody
registered simply never appears — and "loud" here means "defaults to
hiding", which is the direction product design §9 item 8 already chose
("审计记录只保留业务所需的脱敏信息").

**The default whitelist is empty.** Not a placeholder — a decision. No key
that any `append_audit` call in this codebase passes today (`reason`,
`channel`, `url`, `entry`, `prefix`, and dozens more across a dozen modules)
has been reviewed by whoever owns those call sites and declared safe for a
脱敏 reader. Guessing which ones are "probably just metadata" is exactly
the shortcut that turns into next quarter's leak; the honest state of this
list on the day it ships is "nobody has asked for anything yet". Whoever
needs a specific key visible to a viewer adds it here, deliberately, in a
change somebody reviews.
"""

from typing import Any

#: See the module docstring for why this starts empty.
VIEWER_CONTEXT_WHITELIST: frozenset[str] = frozenset()


def redact_context(
    context: dict[str, Any], *, whitelist: frozenset[str] = VIEWER_CONTEXT_WHITELIST
) -> dict[str, Any]:
    """`context`, minus every key not on `whitelist` — key and value both.

    A copy: the caller's own `context` (which may be the very dict an
    `AuditRecord` carries for every other subject reading the same row) is
    never touched by this call.
    """
    return {key: value for key, value in context.items() if key in whitelist}

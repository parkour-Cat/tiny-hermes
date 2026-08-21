"""§3: `end_users` carries no identifiable information, ever.

This is the premise §344's erasure is cheap on: erasing a subject only has to
touch this one table's `erased_at`, never a search for a name or email copied
somewhere else. That premise survives only if nobody adds an identifying
column later because it seemed convenient — an enterprise's profile fields
belong in `external_identities.profile`, scoped to one channel, not here.

Pinned to the table's exact column set rather than to an absence of specific
names: an incomplete blocklist would pass the moment someone added `nickname`
or `handle` instead of `email`.
"""

from tiny_hermes.identity.infrastructure.end_user_tables import EndUserRow


def test_end_users_has_no_identifiable_columns() -> None:
    columns = set(EndUserRow.__table__.columns.keys())

    assert columns == {"id", "workspace_id", "created_at", "erased_at"}

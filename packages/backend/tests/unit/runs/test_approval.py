"""What a person approved, and when an old approval stops covering a new call.

§16.3's rule is one sentence — "a change to the tool name, the arguments, the
working directory, the target resource or the permission invalidates the
approval" — and it is the kind of sentence that becomes four slightly different
comparisons if nobody pins it. So it is pinned exhaustively here: one test per
thing that must invalidate, and one for the single thing that must not.

The one that must not is argument order. A mapping has no order its caller
chose, and a model that emitted the same two arguments the other way round
would otherwise need the same person to approve the same call twice.

The permission rules get the same treatment in both directions. The reverse
test — an administrator answering a user's confirmation — is the one that
matters: §16.3 forbids exactly that, and a platform that allowed it would
afterwards have no way to say which of the two people actually agreed.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tiny_hermes.runs.domain.approval import (
    DEFAULT_VALIDITY,
    MAX_VALIDITY,
    MIN_VALIDITY,
    Approval,
    ApprovalStatus,
    ApprovalType,
    CallNotNormalizable,
    Decider,
    NormalizedCall,
    expires_at,
    is_still_valid,
    may_decide,
    normalize_call,
    validity_window,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def call(
    tool: str = "file.write",
    arguments: dict[str, object] | None = None,
    **rest: str | None,
) -> NormalizedCall:
    return normalize_call(
        tool,
        arguments if arguments is not None else {"path": "report.md", "mode": "replace"},
        **rest,  # pyright: ignore[reportArgumentType]
    )


def approval(
    *,
    approval_type: ApprovalType = ApprovalType.USER_CONFIRMATION,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    content_hash: str | None = None,
    requested_by: UUID | None = None,
    expires: datetime | None = None,
) -> Approval:
    normalized = call()
    return Approval(
        id=uuid4(),
        run_id=uuid4(),
        workspace_id=uuid4(),
        approval_type=approval_type,
        status=status,
        tool="file.write",
        content_hash=content_hash or normalized.content_hash,
        document=normalized.document,
        required_permission=None,
        requested_by=requested_by or uuid4(),
        expires_at=expires or (NOW + timedelta(hours=1)),
    )


# -- what the hash covers ----------------------------------------------------


def test_the_same_call_normalizes_to_the_same_hash() -> None:
    assert call().content_hash == call().content_hash


def test_the_order_the_arguments_arrived_in_is_not_part_of_it() -> None:
    """The one thing that must not invalidate. A model emitting the same two
    arguments the other way round must not need a second approval."""
    first = call(arguments={"path": "report.md", "mode": "replace"})
    second = call(arguments={"mode": "replace", "path": "report.md"})

    assert first.content_hash == second.content_hash


def test_a_different_tool_is_a_different_approval() -> None:
    assert call(tool="file.write").content_hash != call(tool="shell.exec").content_hash


def test_a_changed_argument_is_a_different_approval() -> None:
    changed = call(arguments={"path": "salaries.md", "mode": "replace"})

    assert changed.content_hash != call().content_hash


def test_an_added_argument_is_a_different_approval() -> None:
    """Otherwise a person approves two arguments and a third is sent."""
    changed = call(arguments={"path": "report.md", "mode": "replace", "force": True})

    assert changed.content_hash != call().content_hash


def test_a_removed_argument_is_a_different_approval() -> None:
    changed = call(arguments={"path": "report.md"})

    assert changed.content_hash != call().content_hash


def test_a_different_working_directory_is_a_different_file() -> None:
    first = call(working_directory="/workspace/data")
    second = call(working_directory="/workspace/cache")

    assert first.content_hash != second.content_hash


def test_a_different_target_resource_is_a_different_approval() -> None:
    first = call(target="https://api.example.com/v2/orders")
    second = call(target="https://api.example.com/v2/refunds")

    assert first.content_hash != second.content_hash


def test_a_different_permission_is_a_different_power_being_asked_for() -> None:
    first = call(required_permission="workspace.files.write")
    second = call(required_permission="workspace.secrets.read")

    assert first.content_hash != second.content_hash


def test_omitting_a_field_and_naming_it_are_not_the_same() -> None:
    """`None` and a value are different, so an approval granted for a call with
    no target does not cover the same call once one is added."""
    assert call().content_hash != call(target="somewhere").content_hash


def test_an_integer_and_a_float_are_different_arguments() -> None:
    """What this platform can actually tell apart, and the docstring says so:
    `1` and `1.0` differ, `1.0` and `1.00` are one float by the time they are
    here."""
    assert (
        call(arguments={"limit": 1}).content_hash
        != call(arguments={"limit": 1.0}).content_hash
    )


def test_true_and_one_are_different_arguments() -> None:
    """`isinstance(True, int)` is true, which is exactly the trap this checks."""
    assert (
        call(arguments={"force": True}).content_hash
        != call(arguments={"force": 1}).content_hash
    )


def test_a_nested_object_is_sorted_like_the_top_level() -> None:
    first = call(arguments={"body": {"sku": "a", "qty": 2}})
    second = call(arguments={"body": {"qty": 2, "sku": "a"}})

    assert first.content_hash == second.content_hash


def test_the_order_inside_a_list_is_significant() -> None:
    """Anything that reads a list positionally sees two different arguments,
    and this module cannot know which tools do."""
    first = call(arguments={"paths": ["a", "b"]})
    second = call(arguments={"paths": ["b", "a"]})

    assert first.content_hash != second.content_hash


def test_an_argument_with_no_stable_spelling_is_refused() -> None:
    """Coerced, it would be a hash two runs could disagree about — and a hash
    that is not reproducible approves nothing."""
    with pytest.raises(CallNotNormalizable) as refused:
        call(arguments={"when": datetime.now(UTC)})

    assert "when" in refused.value.detail


def test_the_document_shown_is_the_document_hashed() -> None:
    """The thing a person reviews and the thing the platform enforces come out
    of one normalization, so they cannot drift apart."""
    normalized = call(target="https://api.example.com/v2/orders")

    assert normalized.document["tool"] == "file.write"
    assert normalized.document["target"] == "https://api.example.com/v2/orders"
    assert normalized.document["arguments"]["path"] == "report.md"


# -- whether an approval still covers a call ---------------------------------


def test_an_approved_unexpired_matching_approval_covers_the_call() -> None:
    assert is_still_valid(approval(), call(), NOW)


def test_a_pending_approval_covers_nothing() -> None:
    assert not is_still_valid(approval(status=ApprovalStatus.PENDING), call(), NOW)


def test_a_rejected_approval_covers_nothing() -> None:
    assert not is_still_valid(approval(status=ApprovalStatus.REJECTED), call(), NOW)


def test_an_expired_approval_covers_nothing() -> None:
    stale = approval(expires=NOW - timedelta(seconds=1))

    assert not is_still_valid(stale, call(), NOW)


def test_an_approval_expiring_exactly_now_is_over() -> None:
    """The boundary decided rather than left to a comparison somebody flips."""
    assert not is_still_valid(approval(expires=NOW), call(), NOW)


def test_an_approval_for_a_different_call_covers_nothing() -> None:
    granted = approval()

    assert not is_still_valid(granted, call(arguments={"path": "salaries.md"}), NOW)


# -- who may decide ----------------------------------------------------------


def test_the_run_s_end_user_may_answer_their_own_confirmation() -> None:
    person = uuid4()
    asked = approval(requested_by=person)

    assert may_decide(asked, Decider(user_id=person, is_run_end_user=True))


def test_an_administrator_may_not_answer_a_user_confirmation() -> None:
    """The reverse test §16.3 turns on. An administrator who thinks the action
    should happen opens a governance approval and writes down why; they never
    answer in the user's name."""
    asked = approval(requested_by=uuid4())

    assert not may_decide(
        asked,
        Decider(user_id=uuid4(), is_workspace_admin=True, is_platform_admin=True),
    )


def test_another_end_user_may_not_answer_someone_else_s_confirmation() -> None:
    asked = approval(requested_by=uuid4())

    assert not may_decide(asked, Decider(user_id=uuid4(), is_run_end_user=True))


def test_the_right_person_on_the_wrong_run_may_not_answer() -> None:
    """Being the identity that was asked is not enough: it has to be this Run's
    EndUser, which is the fact the caller passes in."""
    person = uuid4()
    asked = approval(requested_by=person)

    assert not may_decide(asked, Decider(user_id=person, is_run_end_user=False))


def test_a_workspace_admin_may_answer_a_governance_approval() -> None:
    asked = approval(approval_type=ApprovalType.GOVERNANCE_APPROVAL)

    assert may_decide(asked, Decider(user_id=uuid4(), is_workspace_admin=True))


def test_a_platform_admin_may_answer_a_governance_approval() -> None:
    asked = approval(approval_type=ApprovalType.GOVERNANCE_APPROVAL)

    assert may_decide(asked, Decider(user_id=uuid4(), is_platform_admin=True))


def test_an_end_user_may_never_answer_a_governance_approval() -> None:
    person = uuid4()
    asked = approval(
        approval_type=ApprovalType.GOVERNANCE_APPROVAL, requested_by=person
    )

    assert not may_decide(asked, Decider(user_id=person, is_run_end_user=True))


# -- how long one lasts ------------------------------------------------------


def test_an_unconfigured_workspace_gets_twenty_four_hours() -> None:
    assert validity_window(None) == DEFAULT_VALIDITY


def test_a_configured_window_inside_the_bounds_is_kept() -> None:
    assert validity_window(timedelta(hours=2)) == timedelta(hours=2)


def test_a_window_under_the_floor_is_raised_rather_than_failing_a_run() -> None:
    """Read in the middle of somebody's Run. Failing it because a settings page
    holds an out-of-range number punishes the wrong person; the settings page
    is where an impossible value is refused."""
    assert validity_window(timedelta(seconds=1)) == MIN_VALIDITY


def test_a_window_over_the_ceiling_is_lowered() -> None:
    assert validity_window(timedelta(days=90)) == MAX_VALIDITY


def test_the_deadline_is_the_window_from_now() -> None:
    assert expires_at(NOW, timedelta(hours=2)) == NOW + timedelta(hours=2)
    assert expires_at(NOW) == NOW + DEFAULT_VALIDITY

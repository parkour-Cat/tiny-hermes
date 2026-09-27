"""What a parent is told about one child that finished.

`child_result_for` cut a child's last words at `MAX_CHILD_SUMMARY` and wrote
`summary_truncated`, under a comment saying the cut was made "with a number
rather than silently: a parent reading a cut-off sentence cannot tell that it
is holding half of one". Nothing read the flag: the parent was handed the cut
text with no mark on it. These tests are on the line the parent reads.
"""

from uuid import uuid4

from tiny_hermes.runs.infrastructure.sql_store import MAX_CHILD_SUMMARY, child_report_line


def test_a_cut_report_says_it_was_cut_and_how_long_it_was() -> None:
    line = child_report_line(
        uuid4(),
        {
            "status": "completed",
            "summary": "x" * MAX_CHILD_SUMMARY,
            "summary_truncated": True,
            "summary_length": 9_000,
            "artifacts": [],
        },
    )

    assert str(MAX_CHILD_SUMMARY) in line
    assert "9000" in line
    assert "cut" in line


def test_a_report_recorded_before_its_length_was_kept_still_says_it_was_cut() -> None:
    line = child_report_line(
        uuid4(),
        {
            "status": "completed",
            "summary": "x" * MAX_CHILD_SUMMARY,
            "summary_truncated": True,
            "artifacts": [],
        },
    )

    assert "cut" in line


def test_a_whole_report_is_not_marked() -> None:
    line = child_report_line(
        uuid4(), {"status": "completed", "summary": "Done: three files.", "artifacts": []}
    )

    assert "cut" not in line
    assert "Done: three files." in line

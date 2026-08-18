"""The document an approver decides from.

§15.3 step 2 sits between "somebody proposed something" and "somebody approved
it", and it is the only step whose output a human reads closely. So the tests
here are mostly about being readable and being the same twice — a diff that
reorders itself between two openings makes "I reviewed this" mean nothing.
"""

from tiny_hermes.skills.domain.diff import (
    CONTEXT_LINES,
    MAX_FILE_DIFF_LINES,
    FileChange,
    LineKind,
    diff_packages,
)
from tiny_hermes.skills.domain.package import SkillFile


def files(**contents: str) -> tuple[SkillFile, ...]:
    """Named by a python identifier, addressed by the path it stands for."""
    return tuple(
        SkillFile(path=name.replace("__", "/").replace("_", "."), text=text)
        for name, text in contents.items()
    )


def test_two_identical_packages_have_nothing_to_show() -> None:
    same = files(SKILL_md="---\nname: a\n---\n", notes_md="one\n")

    difference = diff_packages(same, same)

    assert difference.empty is True
    assert difference.files == ()


def test_a_new_skill_reads_as_every_file_added() -> None:
    """An empty base is how a proposal for a skill that does not exist yet is
    reviewed, and it needs no special case."""
    proposed = files(SKILL_md="---\nname: a\n---\n", notes_md="one\ntwo\n")

    difference = diff_packages((), proposed)

    assert [item.path for item in difference.files] == ["SKILL.md", "notes.md"]
    assert {item.change for item in difference.files} == {FileChange.ADDED}
    assert difference.added == 2
    notes = difference.files[1]
    assert [line.text for line in notes.lines] == ["one", "two"]
    assert notes.added_lines == 2
    assert notes.removed_lines == 0


def test_a_file_the_proposal_drops_is_shown_as_removed() -> None:
    difference = diff_packages(files(a_md="gone\n"), ())

    assert difference.removed == 1
    only = difference.files[0]
    assert only.change is FileChange.REMOVED
    assert only.removed_lines == 1
    assert [line.kind for line in only.lines] == [LineKind.REMOVED]


def test_a_changed_file_shows_both_sides_of_the_change() -> None:
    difference = diff_packages(files(a_md="one\ntwo\n"), files(a_md="one\ntwo point five\n"))

    only = difference.files[0]
    assert only.change is FileChange.CHANGED
    assert only.added_lines == 1
    assert only.removed_lines == 1
    assert [(line.kind, line.text) for line in only.lines] == [
        (LineKind.CONTEXT, "one"),
        (LineKind.REMOVED, "two"),
        (LineKind.ADDED, "two point five"),
    ]


def test_files_come_back_in_path_order_whatever_order_they_arrived_in() -> None:
    """Determinism starts here: two reads must agree, and dictionaries do not."""
    base = files(zebra_md="z\n", alpha_md="a\n")
    proposed = files(middle_md="m\n", alpha_md="A\n", zebra_md="Z\n")

    paths = [item.path for item in diff_packages(base, proposed).files]

    assert paths == ["alpha.md", "middle.md", "zebra.md"]
    assert paths == [item.path for item in diff_packages(base, proposed).files]


def test_a_long_unchanged_stretch_collapses_and_says_how_much_it_hid() -> None:
    """The reader has to be able to tell that two hunks are not adjacent."""
    before = "\n".join(str(number) for number in range(40)) + "\n"
    after = before.replace("0\n", "zero\n", 1)

    only = diff_packages(files(a_md=before), files(a_md=after)).files[0]

    kinds = [line.kind for line in only.lines]
    assert LineKind.SKIPPED in kinds
    skipped = next(line for line in only.lines if line.kind is LineKind.SKIPPED)
    assert "unchanged lines" in skipped.text
    assert kinds.count(LineKind.CONTEXT) == CONTEXT_LINES * 2


def test_a_short_unchanged_stretch_is_shown_whole() -> None:
    """Collapsing four lines to "3 unchanged lines" costs a reader more than it
    saves them."""
    only = diff_packages(files(a_md="a\nb\nc\n"), files(a_md="A\nb\nc\n")).files[0]

    assert [line.kind for line in only.lines] == [
        LineKind.REMOVED,
        LineKind.ADDED,
        LineKind.CONTEXT,
        LineKind.CONTEXT,
    ]


def test_a_huge_file_stops_but_still_reports_the_whole_count() -> None:
    """Truncation here is a rendering limit told to a person, not the context
    planner's rule — and the numbers it reports stay the file's real ones, so a
    reviewer is never told less changed than did."""
    body = "".join(f"line {number}\n" for number in range(MAX_FILE_DIFF_LINES + 50))

    only = diff_packages((), files(a_md=body)).files[0]

    assert only.truncated is True
    assert len(only.lines) == MAX_FILE_DIFF_LINES
    assert only.added_lines == MAX_FILE_DIFF_LINES + 50


def test_a_file_without_a_trailing_newline_is_not_given_a_phantom_line() -> None:
    only = diff_packages((), files(a_md="one")).files[0]

    assert only.added_lines == 1
    assert [line.text for line in only.lines] == ["one"]


def test_a_file_that_only_gained_a_trailing_newline_is_still_a_change() -> None:
    """It is a content change, so it is a new content hash, so it is a version.
    Hiding it here would show a reviewer an empty diff for a real difference."""
    difference = diff_packages(files(a_md="one"), files(a_md="one\n"))

    assert difference.empty is False

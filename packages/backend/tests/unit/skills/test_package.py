"""What a skill package has to be before anything will store one.

Product design §15.1. Every refusal here is named, because the person who gets
one is holding a directory of files and needs to know which file to change. A
single `invalid package` would send them reading their own tarball byte by byte.

The other property this file pins is the content hash: the same files in a
different order, or arriving by a different route, must hash the same. §3's
"importing the same content twice does not make a second version" is that
property and nothing else.
"""

import pytest
from tiny_hermes.skills.domain.package import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PACKAGE_BYTES,
    MAX_SKILL_MD_BYTES,
    SUMMARY_MAX_LENGTH,
    SkillFile,
    SkillPackageRefused,
    content_hash,
    parse_package,
)

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Read `CHANGELOG.md`, group by change kind, and write one paragraph per group.
"""


def files(*entries: tuple[str, str]) -> tuple[SkillFile, ...]:
    return tuple(SkillFile(path=path, text=text) for path, text in entries)


def package(*extra: tuple[str, str]) -> tuple[SkillFile, ...]:
    return files(("SKILL.md", SKILL_MD), *extra)


def test_a_package_states_its_name_and_what_it_is_for() -> None:
    parsed = parse_package(package())
    assert parsed.manifest.name == "release-notes"
    assert parsed.manifest.description.startswith("Turn a changelog")
    assert [entry.path for entry in parsed.files] == ["SKILL.md"]


def test_the_files_come_back_in_one_order_whatever_order_they_arrived_in() -> None:
    forwards = parse_package(package(("a.md", "one"), ("b.md", "two")))
    backwards = parse_package(files(("b.md", "two"), ("a.md", "one"), ("SKILL.md", SKILL_MD)))
    assert [entry.path for entry in forwards.files] == ["SKILL.md", "a.md", "b.md"]
    assert forwards.files == backwards.files
    assert forwards.content_hash == backwards.content_hash


def test_content_that_differs_anywhere_hashes_differently() -> None:
    first = parse_package(package(("a.md", "one")))
    second = parse_package(package(("a.md", "one ")))
    renamed = parse_package(package(("b.md", "one")))
    assert len({first.content_hash, second.content_hash, renamed.content_hash}) == 3


def test_the_hash_is_of_the_content_not_of_the_parse() -> None:
    """So an import route that never calls the parser still agrees with one that did."""
    entries = parse_package(package(("a.md", "one"))).files
    assert content_hash(entries) == parse_package(entries).content_hash


def test_a_package_without_a_skill_md_has_nothing_to_say_for_itself() -> None:
    with pytest.raises(SkillPackageRefused, match="SKILL.md"):
        parse_package(files(("README.md", "hello")))


def test_a_skill_md_without_frontmatter_is_refused() -> None:
    with pytest.raises(SkillPackageRefused, match="frontmatter"):
        parse_package(files(("SKILL.md", "# Just a heading\n")))


@pytest.mark.parametrize("missing", ["name", "description"])
def test_both_manifest_fields_are_required(missing: str) -> None:
    lines = [line for line in SKILL_MD.splitlines() if not line.startswith(f"{missing}:")]
    with pytest.raises(SkillPackageRefused, match=missing):
        parse_package(files(("SKILL.md", "\n".join(lines))))


def test_a_description_longer_than_the_segment_can_afford_is_refused() -> None:
    """The cap is arithmetic, not taste: see `SUMMARY_MAX_LENGTH`'s comment."""
    long = SKILL_MD.replace(
        "Turn a changelog into release notes in this company's house style.",
        "x" * (SUMMARY_MAX_LENGTH + 1),
    )
    with pytest.raises(SkillPackageRefused, match="description"):
        parse_package(files(("SKILL.md", long)))


@pytest.mark.parametrize("path", ["../escape.md", "/etc/passwd", "a/../../b.md", "a\\b.md"])
def test_a_path_that_could_leave_the_package_is_refused(path: str) -> None:
    with pytest.raises(SkillPackageRefused, match="path"):
        parse_package(package((path, "hello")))


def test_the_same_path_twice_is_refused_rather_than_resolved() -> None:
    """Which of the two bodies won would be an accident of ordering."""
    with pytest.raises(SkillPackageRefused, match="once"):
        parse_package(package(("a.md", "one"), ("a.md", "two")))


def test_a_package_with_too_many_files_is_refused() -> None:
    many = tuple((f"note-{index}.md", "x") for index in range(MAX_FILES))
    with pytest.raises(SkillPackageRefused, match="files"):
        parse_package(package(*many))


def test_one_oversized_file_is_refused_by_name() -> None:
    with pytest.raises(SkillPackageRefused, match="big.md"):
        parse_package(package(("big.md", "x" * (MAX_FILE_BYTES + 1))))


def test_a_skill_md_has_a_tighter_ceiling_than_the_files_beside_it() -> None:
    assert MAX_SKILL_MD_BYTES < MAX_FILE_BYTES
    body = SKILL_MD + "x" * MAX_SKILL_MD_BYTES
    with pytest.raises(SkillPackageRefused, match="SKILL.md"):
        parse_package(files(("SKILL.md", body)))


def test_a_package_over_its_total_is_refused_even_when_every_file_fits() -> None:
    each = "x" * MAX_FILE_BYTES
    count = MAX_PACKAGE_BYTES // MAX_FILE_BYTES + 1
    entries = tuple((f"note-{index}.md", each) for index in range(count))
    assert count <= MAX_FILES, "this case must fail on the total, not the count"
    with pytest.raises(SkillPackageRefused, match="package"):
        parse_package(package(*entries))


def test_the_byte_ceilings_are_measured_in_bytes_not_characters() -> None:
    """A cap counted in characters is three times looser for Chinese text."""
    text = "字" * MAX_FILE_BYTES
    with pytest.raises(SkillPackageRefused, match="wide.md"):
        parse_package(package(("wide.md", text)))


def test_a_nul_byte_is_refused_where_the_file_is_still_named() -> None:
    """PostgreSQL text cannot hold one, and a store error names no file."""
    with pytest.raises(SkillPackageRefused, match="notes.md"):
        parse_package(package(("notes.md", "before\x00after")))


def test_a_file_hashes_the_same_however_its_lines_ended() -> None:
    windows = parse_package(package(("a.md", "one\r\ntwo\r\n")))
    unix = parse_package(package(("a.md", "one\ntwo\n")))
    assert windows.content_hash == unix.content_hash


def test_a_frontmatter_field_nobody_reads_is_refused_not_dropped() -> None:
    """A field the platform ignores is a field its author believes works."""
    extra = SKILL_MD.replace("---\n\n#", "tools: shell.exec\n---\n\n#")
    with pytest.raises(SkillPackageRefused, match="tools"):
        parse_package(files(("SKILL.md", extra)))


def test_a_name_the_model_would_have_to_quote_is_refused() -> None:
    odd = SKILL_MD.replace("name: release-notes", 'name: "Release Notes!"')
    with pytest.raises(SkillPackageRefused, match="name"):
        parse_package(files(("SKILL.md", odd)))

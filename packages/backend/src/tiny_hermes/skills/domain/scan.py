"""What a static scan of a skill package can honestly say.

Product design §15.3 makes a scan a precondition of publishing. This module is
deliberate about how little it claims.

**It is not a sandbox and not a malware detector.** A SKILL.md whose entire
content is an instruction to disregard the platform's rules passes this scan,
and a test pins that. The guards against that content are elsewhere and are the
ones worth keeping: skill text enters the prompt marked as reference material,
every tool call the model makes afterwards still goes through §16.2's two
permission checks, and commands still run in a container with no network. A
scanner that advertised itself as catching hostile prose is exactly how those
three stop being maintained.

**Only one finding blocks.** Credential material does, because a catalog that
accepts a private key will serve it into a prompt and hand it to a model. The
rest are things a reviewer should see and decide about; blocking on all of them
would train people to write packages that dodge the scanner.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from tiny_hermes.skills.domain.package import MANIFEST_PATH, SkillFile


class Severity(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    path: str
    #: One sentence, for a person deciding whether to approve. Not a stack
    #: trace and not a rule id on its own.
    detail: str


#: Shapes that are credentials whatever they are called. Each is specific
#: enough that a match is evidence rather than a hint.
_SECRETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "a secret assigned to a name that says so",
        # The value has to look like one too: `password: ask the vault` is
        # prose, and blocking on prose teaches people to phrase it differently
        # rather than to stop committing keys.
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9/+_.-]{20,}"
        ),
    ),
)

#: Extensions whose contents are meant to be executed rather than read.
_SCRIPTS = frozenset({".sh", ".bash", ".py", ".js", ".rb", ".ps1"})

_REACHES_OUT = re.compile(r"https?://|\b\d{1,3}(?:\.\d{1,3}){3}\b")

#: Markdown links only. A backtick-quoted path is as often an example as a
#: reference, and a scan that guessed would spend its findings on prose.
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def scan(files: Sequence[SkillFile]) -> tuple[Finding, ...]:
    """Every finding, ordered by path then code.

    Deterministic: a reviewer comparing this version's scan with the last one
    must be reading a difference in the package, not a reshuffle.
    """
    present = {entry.path for entry in files}
    referenced: set[str] = set()
    findings: list[Finding] = []

    for entry in files:
        findings.extend(_credentials(entry))
        if _is_script(entry.path) and _REACHES_OUT.search(entry.text):
            findings.append(
                Finding(
                    code="network_in_script",
                    severity=Severity.ADVISORY,
                    path=entry.path,
                    detail=(
                        "reaches a network address; the sandbox this would run in "
                        "has none, so the command will fail"
                    ),
                )
            )
        if not entry.path.endswith(".md"):
            continue
        for target in _links(entry):
            referenced.add(target)
            if target not in present:
                findings.append(
                    Finding(
                        code="missing_reference",
                        severity=Severity.ADVISORY,
                        path=entry.path,
                        detail=f"links to {target}, which is not in this package",
                    )
                )

    for entry in files:
        if entry.path == MANIFEST_PATH or entry.path in referenced:
            continue
        findings.append(
            Finding(
                code="unreferenced_file",
                severity=Severity.ADVISORY,
                path=entry.path,
                detail="nothing in this package links to it, so no Run will reach it",
            )
        )

    return tuple(sorted(findings, key=lambda finding: (finding.path, finding.code)))


def blocking(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """The findings that stop a publication. Empty is the precondition."""
    return tuple(finding for finding in findings if finding.severity is Severity.BLOCKING)


def _credentials(entry: SkillFile) -> list[Finding]:
    return [
        Finding(
            code="credential_material",
            severity=Severity.BLOCKING,
            path=entry.path,
            detail=f"contains what looks like {what}",
        )
        for what, pattern in _SECRETS
        if pattern.search(entry.text)
    ]


def _is_script(path: str) -> bool:
    return any(path.endswith(extension) for extension in _SCRIPTS)


def _links(entry: SkillFile) -> list[str]:
    """Package-relative targets, with anything that leaves the package dropped.

    A link out of the package is not a broken reference to report — it is a URL
    or an escape, and neither is a file this scan has an opinion about.
    """
    base = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
    targets: list[str] = []
    for raw in _LINK.findall(entry.text):
        target = raw.split("#", 1)[0].split("?", 1)[0]
        if not target or "://" in target or target.startswith(("/", "mailto:")):
            continue
        resolved = _resolve(base, target)
        if resolved is not None:
            targets.append(resolved)
    return targets


def _resolve(base: str, target: str) -> str | None:
    parts: list[str] = base.split("/") if base else []
    for piece in target.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts) or None

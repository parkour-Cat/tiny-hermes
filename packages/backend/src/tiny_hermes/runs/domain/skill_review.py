"""The post-run review's prompt and the one reply it accepts (§15.4, v2.12).

Pure. The Worker decides whether to ask and what to do with the answer; this
module only says what is asked and what counts as an answer.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

#: The first line of every review prompt — how the model call is told apart
#: from an ordinary round, in logs and in tests.
REVIEW_MARKER = "Review a finished task for a reusable skill."

_FENCE = "```"


class ReviewUnreadable(ValueError):
    """The reply was not the one JSON object the prompt asked for."""


@dataclass(frozen=True)
class ReviewDecision:
    decision: Literal["none", "new", "patch"]
    #: The bound skill a patch changes; `None` otherwise.
    skill: str | None = None
    #: The whole proposed SKILL.md; `None` for "none".
    skill_md: str | None = None


def review_prompt(transcript: str, skills: Sequence[tuple[str, str]]) -> str:
    """What the reviewer is asked. ``skills`` are the bound ones, as
    (name, description) — the only ones a patch may name."""
    bound = (
        "\n".join(f"- {name}: {description}" for name, description in skills)
        if skills
        else "(none)"
    )
    return f"""{REVIEW_MARKER}

Below is the record of a task an agent just finished. Decide whether it taught
a procedure worth keeping as a skill — steps someone doing similar work next
time would want written down. Most tasks do not; a one-off answer, a lookup, or
work that followed an existing skill unchanged is not one.

Skills this agent already has:
{bound}

Reply with exactly one JSON object and nothing else:
- {{"decision": "none"}} when nothing is worth keeping;
- {{"decision": "new", "skill_md": "<the whole SKILL.md>"}} for a new skill;
- {{"decision": "patch", "skill": "<name from the list above>",
   "skill_md": "<the whole new SKILL.md>"}}
  when an existing skill was missing a step or got one wrong.

A SKILL.md starts with front matter holding `name` (lowercase words joined by
hyphens) and `description` (one sentence on when to use it), then the steps.
Never put credentials, hostnames' secrets, or anything a user said about
themselves into it. A person will review it before anyone can use it.

The record:
{transcript}"""


def parse_review(reply: str) -> ReviewDecision:
    """The decision, or `ReviewUnreadable`. A code fence around the object is
    the one liberty taken; anything else is refused rather than guessed at."""
    text = reply.strip()
    if text.startswith(_FENCE) and text.endswith(_FENCE):
        text = text[len(_FENCE) : -len(_FENCE)].strip()
        if text.startswith("json"):
            text = text[len("json") :].strip()
    try:
        parsed: Any = json.loads(text)
    except ValueError as error:
        raise ReviewUnreadable("not JSON") from error
    if not isinstance(parsed, dict):
        raise ReviewUnreadable("not an object")
    fields = cast(dict[str, Any], parsed)
    decision = fields.get("decision")
    if decision == "none":
        return ReviewDecision("none")
    skill_md = fields.get("skill_md")
    if not isinstance(skill_md, str) or not skill_md.strip():
        raise ReviewUnreadable("no skill_md")
    if decision == "new":
        return ReviewDecision("new", skill_md=skill_md)
    if decision == "patch":
        skill = fields.get("skill")
        if not isinstance(skill, str) or not skill.strip():
            raise ReviewUnreadable("a patch names no skill")
        return ReviewDecision("patch", skill=skill, skill_md=skill_md)
    raise ReviewUnreadable("unknown decision")

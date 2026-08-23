"""The Agent a fresh deployment can create without deciding anything first.

§26 lists 示例 Agent beside the licence and the contribution guide, and
§21's wizard makes it the last of five steps — after the first administrator,
login, a model alias and a workspace. So the one thing it must be is
*runnable at the end of that wizard*, on a deployment that has nothing else:
no HTTP tool registered, no MCP server reachable, no host on the network
allow-list, no skill published.

That rules out most of what would make a flashy demo, and what is left is
the part worth showing anyway. This Agent reads files, writes one file, and
declares what "finished" means — `expected_artifacts`, which the platform
*checks*. A model that says it is done and has not written the file does not
finish the Run (§12.2). An example whose completion the platform simply
believed would teach a reader the opposite of how this platform works.

The spec is here rather than in a JSON file under `examples/` so that it is
validated by the same `AgentSpec` every published version goes through, in
the same process, at import time if anything is wrong — a sample file can
rot against a schema for a release and nobody notices until someone imports
it. `docs/architecture.md` points readers at this module.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AgentExample:
    """One ready-made Agent, and the words the console shows about it."""

    slug: str
    name: str
    alias: str
    #: One sentence, shown beside the button. Deliberately says what it does
    #: rather than what it demonstrates: an administrator clicking this at the
    #: end of a wizard wants to know what they are about to create.
    summary: str

    def spec(self, endpoint_id: UUID) -> dict[str, Any]:
        """The spec, bound to this deployment's own model endpoint.

        Taken as an argument rather than chosen here: which endpoint an
        example should use is a fact about the deployment, and an example
        that picked one itself would silently bind to whichever row happened
        to be first.
        """
        return {
            "schema_version": 1,
            "personality": (
                "You tidy rough notes into a short written summary.\n\n"
                "Read every file under /workspace/data/notes. Write one file, "
                "/workspace/data/summary.md, containing: a one-paragraph "
                "overview, then a bulleted list of decisions, then a bulleted "
                "list of open questions with the person named where the notes "
                "name one. Quote the notes rather than inventing detail, and "
                "if the notes do not settle something, put it under open "
                "questions instead of guessing.\n\n"
                "You are finished when summary.md exists and reflects every "
                "note file you read."
            ),
            "model_policy": {
                "provider": "openai_compatible",
                "endpoint_id": str(endpoint_id),
            },
            # Read and list as well as write: an Agent that could only write
            # would have to be told the file names, which makes the example a
            # script rather than a task.
            "tools": ["file.list", "file.read", "file.write"],
            "completion": {
                # The point of the example. The model's claim to be done is
                # not the end of it — this file must exist.
                "expected_artifacts": ["summary.md"],
                "constraints": (
                    "Do not invent facts the notes do not contain. Unresolved "
                    "points belong under open questions."
                ),
                "stop_conditions": {"max_rounds": 12},
            },
            # No `network` key at all: absent means nothing, and an example
            # that asked for the network would need a workspace allow-list to
            # exist before it could be published.
            "limits": {
                "max_execution_seconds": 300,
                "max_elapsed_seconds": 3600,
                "max_model_calls": 20,
                "max_tool_calls": 40,
                "max_derived_retries": 2,
            },
        }


NOTES_TIDIER = AgentExample(
    slug="notes-tidier",
    name="Notes tidier",
    alias="notes-tidier",
    summary=(
        "Reads the note files in a session's workspace and writes one "
        "summary.md — with decisions and open questions kept apart. The "
        "platform checks that the file was actually written before the Run "
        "is allowed to finish."
    ),
)

#: Every example this platform ships. A tuple rather than one constant
#: because §21 says "create or import", and the day a second one is added
#: nothing about the route or the console has to change shape.
EXAMPLES: tuple[AgentExample, ...] = (NOTES_TIDIER,)


def example_for(slug: str) -> AgentExample | None:
    return next((item for item in EXAMPLES if item.slug == slug), None)

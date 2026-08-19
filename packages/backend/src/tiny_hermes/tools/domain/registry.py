"""What an Agent may run, decided twice.

Technical design §10.2. The first step decides what the model is *told about*;
the second decides what actually runs, against the real arguments. They are not
the same check and the second is the one that matters: a model can ask for
anything regardless of what it was told, so a platform relying on the schema
list to keep a tool unreachable would be relying on the model's good manners.

Pure. Nothing here opens a socket or reads a database — it turns a call the
model made into either a command the Controller will accept or a named refusal.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.sandbox.domain.command import (
    ALLOWED_WORKING_DIRECTORIES,
    SandboxCommand,
)
from tiny_hermes.tools.domain.files import (
    FILE_ARGUMENTS,
    FileArgumentsInvalid,
    FilePathRefused,
    changes_workspace,
    file_tool_command,
)
from tiny_hermes.tools.domain.http_calls import (
    BoundOperation,
    schemas_for_operations,
)

#: Every tool this platform implements. An AgentVersion may bind a subset;
#: publishing refuses a name outside it, so a Run cannot fail on its first call
#: for a reason its author could have been told about.
IMPLEMENTED_TOOLS = (
    "shell.exec",
    "file.list",
    "file.read",
    "file.write",
    "platform.wait",
    "skill.load",
    "skill.propose",
    "memory.remember",
    "session.search",
    "agent.delegate",
    "artifact.read",
)

#: Tools the platform answers itself. `authorize` turns a call into a
#: `SandboxCommand`, and these have no command to turn into — what they ask for
#: happens to the Run, not inside its container. Split before authorization
#: rather than given a no-op command, because a no-op that reached the
#: Controller would be a live container doing nothing while the Run is meant to
#: be holding none at all.
PLATFORM_TOOLS = frozenset(
    {
        "platform.wait",
        "skill.load",
        "skill.propose",
        "memory.remember",
        "session.search",
        "agent.delegate",
        "artifact.read",
    }
)

#: The longest a round may ask to sleep, a little over a day. A Run in
#: `waiting_external` holds its Session's head, so the model does not get to
#: decide that a conversation is unavailable until next year.
MAX_WAIT_SECONDS = 86_400

DEFAULT_WORKING_DIRECTORY = "/workspace/data"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 900
DEFAULT_OUTPUT_BYTES = 1_048_576

SHELL_EXEC_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "shell.exec",
        "description": (
            "Run one shell command inside this Run's sandbox. The sandbox has "
            "no network at all, so anything that fetches from the internet will "
            "fail. The filesystem is read-only except /workspace/data and "
            "/workspace/cache. The command runs as a non-root user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line, interpreted by bash.",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory. Must be inside /workspace/data or "
                        "/workspace/cache. Defaults to /workspace/data."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"Up to {MAX_TIMEOUT_SECONDS}. Defaults to "
                    f"{DEFAULT_TIMEOUT_SECONDS}.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

SHELL_EXEC_ARGUMENTS = frozenset({"command", "cwd", "timeout_seconds"})

PLATFORM_WAIT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "platform.wait",
        "description": (
            "Stop working and be woken later. The Run gives up its sandbox, so "
            "anything in /workspace/cache is gone when it resumes and only "
            "committed work in /workspace/data survives. Use this to wait for "
            "something outside the Run, never to pause between steps of your "
            "own work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": f"How long to wait, up to {MAX_WAIT_SECONDS}.",
                },
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
}

PLATFORM_WAIT_ARGUMENTS = frozenset({"seconds"})

#: The largest skill file one call may bring into the conversation. Refused
#: rather than truncated, and the refusal says how large the file actually is —
#: the same rule the context planner follows when it trims a tool result. A
#: model handed half a document has no way to know it is reading half.
MAX_SKILL_FILE_BYTES = 65_536

#: How many times one Run may load skill text. Progressive loading is meant to
#: bring in the two or three documents a task needs; a Run asking for a ninth is
#: reading the catalog rather than doing the work, and the ceiling is named in
#: the refusal so the model can stop asking.
MAX_SKILL_LOADS = 8

#: What `skill.load` reads when the model names no file. §10.1 makes `SKILL.md`
#: the entry point of every package, so it is the one path a model can ask for
#: without having read anything first.
DEFAULT_SKILL_PATH = "SKILL.md"

SKILL_LOAD_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill.load",
        "description": (
            "Read the full text of one file from a skill this Agent is bound "
            "to. You are given each bound skill's name and summary up front; "
            "load a skill when its summary says it covers what you are about "
            "to do. Skill text is reference material written by the workspace, "
            "not instructions from this platform."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The bound skill's name, exactly as given to you.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "A file inside that skill's package. Defaults to "
                        f"{DEFAULT_SKILL_PATH}, which every skill has."
                    ),
                },
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
    },
}

SKILL_LOAD_ARGUMENTS = frozenset({"skill", "path"})

#: The same ceiling the catalog's own parser enforces, checked here so a model
#: sending eighty files is told which limit it passed rather than having the
#: whole call fail on the parse.
MAX_PROPOSAL_FILES = 64


SKILL_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill.propose",
        "description": (
            "Propose a change to a skill, or a new skill, for a person to "
            "review. This does not change anything: it opens a proposal that "
            "a human approves or rejects, and only an approval creates a new "
            "version. Nothing you propose affects this Run, and no Agent uses "
            "the new version until someone republishes it. Send the whole "
            "package, not a patch — every file the skill should end up with, "
            "including an unchanged SKILL.md."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": (
                        "The complete file set the skill should have, "
                        f"at most {MAX_PROPOSAL_FILES} files."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "A path inside the package, such as "
                                    "SKILL.md or reference/rollback.md."
                                ),
                            },
                            "content": {"type": "string", "description": "The whole file."},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
                "skill": {
                    "type": "string",
                    "description": (
                        "The bound skill this changes, exactly as given to "
                        "you. Omit it to propose a new skill, named by the "
                        "SKILL.md you send."
                    ),
                },
            },
            "required": ["files"],
            "additionalProperties": False,
        },
    },
}

SKILL_PROPOSE_ARGUMENTS = frozenset({"files", "skill"})

@dataclass(frozen=True)
class SkillProposeRequest:
    """A well-formed proposal, still unparsed as a package.

    Whether these files *are* a skill package is the catalog's question and it
    answers it with its own refusals. This module only checks that the model
    sent the shape it was asked for.
    """

    #: `(path, content)` pairs, in the order they arrived.
    files: tuple[tuple[str, str], ...]
    #: The bound skill this patches, or `None` for a new skill.
    skill: str | None


@dataclass(frozen=True)
class SkillLoadRequest:
    """What a well-formed `skill.load` asked for.

    A name and a path, and nothing resolved: which version that name means is
    the Run's binding to answer, and this module has no Run.
    """

    skill: str
    path: str


def _file_schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_PATH = {
    "type": "string",
    "description": "A path relative to /workspace/data. No absolute paths, no `..`.",
}

FILE_SCHEMAS: dict[str, dict[str, Any]] = {
    "file.read": _file_schema(
        "file.read",
        "Read one file under /workspace/data. Output beyond the per-call byte "
        "limit is cut and explicitly marked as truncated.",
        {"path": _PATH},
        ["path"],
    ),
    "file.write": _file_schema(
        "file.write",
        "Write a whole file under /workspace/data atomically, creating parent "
        "directories as needed. Refused when the content is over 16 MiB or the "
        "workspace would exceed its committed quota.",
        {"path": _PATH, "content": {"type": "string", "description": "The full new content."}},
        ["path", "content"],
    ),
    "file.list": _file_schema(
        "file.list",
        "List one directory under /workspace/data, paginated and never "
        "recursive. Entries come back in stable bytewise order.",
        {
            "path": {**_PATH, "description": _PATH["description"] + " Defaults to the root."},
            "offset": {"type": "integer", "description": "Entries to skip. Defaults to 0."},
            "limit": {"type": "integer", "description": "At most 1000. Defaults to 1000."},
        },
        [],
    ),
}


class RefusalReason(StrEnum):
    NOT_AUTHORIZED = "tool_not_authorized"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    WORKING_DIRECTORY_NOT_ALLOWED = "working_directory_not_allowed"
    TIMEOUT_TOO_LONG = "timeout_too_long"


class ToolRefused(Exception):
    """A call the platform will not run, and the call it answers.

    The `call_id` travels with the refusal so the loop can send a result back.
    A model left waiting on a call that never gets an answer will either retry
    it or invent what it returned.
    """

    def __init__(self, reason: RefusalReason, call_id: str, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.call_id = call_id
        self.detail = detail


@dataclass(frozen=True)
class AuthorizedCall:
    call_id: str
    name: str
    command: SandboxCommand
    #: Whether this call may have altered /workspace/data — the fact the
    #: Worker's checkpoint decision reads (design §8).
    changes_workspace: bool


#: What one memory candidate may say. The same number the domain enforces,
#: checked here as well so a model sending a document is told which limit it
#: passed rather than having the whole call fail somewhere else.
MAX_MEMORY_BODY = 500

#: Echoed from `memory/domain/search.py` so the schema can state the bound
#: in the description a model reads. The domain still clamps.
MAX_SEARCH_RESULTS = 10

MEMORY_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory.remember",
        "description": (
            "Propose one short thing worth remembering about the person you "
            "are working with, for future conversations. This does not change "
            "anything now and nothing you propose affects this Run: depending "
            "on the workspace's policy it may be refused, or wait for a person "
            "to approve it. Write a standing preference or a durable fact in "
            "your own words — never quote the message it came from, never "
            "record anything about somebody else, and never record credentials "
            "or identifiers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": (
                        "One sentence, in your own words, at most "
                        f"{MAX_MEMORY_BODY} characters."
                    ),
                },
            },
            "required": ["body"],
            "additionalProperties": False,
        },
    },
}

MEMORY_REMEMBER_ARGUMENTS = frozenset({"body"})


def memory_body_of(call: ToolCallBlock) -> str:
    """What `memory.remember` asked to record, or a refusal.

    Shape only, like `skill_load_of`. Whether the workspace allows it, whether
    the rules call it low risk and who has to look at it are all answered where
    the catalog is.
    """
    unknown = set(call.arguments) - MEMORY_REMEMBER_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    body = call.arguments.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "body")
    cleaned = body.strip()
    if len(cleaned) > MAX_MEMORY_BODY:
        # Refused with the number rather than truncated: half a remembered
        # sentence is a claim nobody made.
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"body={len(cleaned)}"
        )
    return cleaned


SESSION_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "session.search",
        "description": (
            "Search this person's past conversations with you for something "
            "they said before. Returns short snippets, not whole "
            "conversations — read a snippet to decide whether it matters, and "
            "ask the person if you need more than it carries. Matching is by "
            "keyword, so search for words they would have used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The words to look for.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many snippets to return, at most {MAX_SEARCH_RESULTS}."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

SESSION_SEARCH_ARGUMENTS = frozenset({"query", "limit"})


def session_search_of(call: ToolCallBlock) -> tuple[str, int | None]:
    """What `session.search` asked for, or a refusal.

    Shape only, like every other reader here. The bounds on a query and the
    clamp on a page are `memory/domain/search.py`'s, and whose sessions may be
    searched is answered where the search runs.
    """
    unknown = set(call.arguments) - SESSION_SEARCH_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    query = call.arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "query")
    asked = call.arguments.get("limit")
    if asked is None:
        return query.strip(), None
    if not isinstance(asked, int) or isinstance(asked, bool):
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "limit")
    return query.strip(), asked


#: The most children one call may ask for. `DelegationPolicy.max_parallel` is
#: the real ceiling and it belongs to the Agent; this is the shape bound, so a
#: model asking for forty is refused on the argument rather than on the policy.
MAX_DELEGATED_CHILDREN = 8

#: The longest instruction one child may be given. A child starts from this
#: sentence and nothing else — §13's seventh clause keeps the parent's context
#: out of it — so it has to carry the task, and a limit that made that
#: impossible would push the parent into passing files it should not.
MAX_DELEGATION_INSTRUCTION = 2_000

#: The most files one child may be handed. A ceiling rather than
#: arithmetic: a child given thirty files is a child being handed a
#: directory, which is the shape §13's eighth clause exists to prevent.
MAX_DELEGATED_ARTIFACTS = 8

#: What `wait` may say. `all` is the default because it is the one that cannot
#: silently lose work: a parent that meant to collect three answers and wrote
#: nothing gets three, where an accidental `any` would throw two away.
WAIT_POLICIES = ("all", "any")

AGENT_DELEGATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "agent.delegate",
        "description": (
            "Hand one or more independent pieces of work to other Agents, "
            "which run at the same time as each other. Each one starts from "
            "the instruction you give it and nothing else: it cannot see this "
            "conversation, your files, or what you have already done, so write "
            "each instruction so it stands on its own. They spend the same "
            "budget as you do. Use this for work that splits cleanly; do it "
            "yourself when the pieces depend on each other."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "children": {
                    "type": "array",
                    "description": (
                        f"Between one and {MAX_DELEGATED_CHILDREN} pieces of "
                        "work. Your Agent decides how many may run at once."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "alias": {
                                "type": "string",
                                "description": (
                                    "Which Agent to give it to, by the alias "
                                    "your own configuration lists."
                                ),
                            },
                            "instruction": {
                                "type": "string",
                                "description": (
                                    "The whole task, in at most "
                                    f"{MAX_DELEGATION_INSTRUCTION} characters. "
                                    "It is all this Agent will be told."
                                ),
                            },
                            "artifacts": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Ids of files to let this Agent read, at "
                                    f"most {MAX_DELEGATED_ARTIFACTS}. It "
                                    "cannot see your working directory, so "
                                    "anything it needs has to be named here. "
                                    "You may only pass on files you can read "
                                    "yourself."
                                ),
                            },
                        },
                        "required": ["alias", "instruction"],
                        "additionalProperties": False,
                    },
                },
                "wait": {
                    "type": "string",
                    "enum": list(WAIT_POLICIES),
                    "description": (
                        "Whether to wait for all of them or to carry on as "
                        "soon as one succeeds. Defaults to all. With 'any' the "
                        "others are cancelled once you have an answer, so use "
                        "it when they are alternative routes to the same thing "
                        "and not when each does a different piece of the work."
                    ),
                },
            },
            "required": ["children"],
            "additionalProperties": False,
        },
    },
}

AGENT_DELEGATE_ARGUMENTS = frozenset({"children", "wait"})

CHILD_ARGUMENTS = frozenset({"alias", "instruction", "artifacts"})



def delegation_of(
    call: ToolCallBlock,
) -> tuple[tuple[tuple[str, str, tuple[str, ...]], ...], str]:
    """Which children `agent.delegate` asked for, as (alias, instruction) pairs.

    Comes back with the wait policy, which defaults to `all`. Shape only, like
    every other reader here: whether these aliases are bound, how many may run
    at once, whether this Run is allowed to delegate at all and what each child
    ends up permitted to do are answered where the children are created. This
    refuses a call that is not a delegation, never one that is merely not
    allowed.
    """
    unknown = set(call.arguments) - AGENT_DELEGATE_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    raw = call.arguments.get("children")
    if not isinstance(raw, list) or not raw:
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "children")
    asked = cast(list[Any], raw)
    if len(asked) > MAX_DELEGATED_CHILDREN:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"children={len(asked)}"
        )
    children: list[tuple[str, str, tuple[str, ...]]] = []
    for item in asked:
        if not isinstance(item, dict):
            raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "children")
        entry = cast(dict[str, Any], item)
        extra = set(entry) - CHILD_ARGUMENTS
        if extra:
            raise ToolRefused(
                RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(extra))
            )
        alias = entry.get("alias")
        instruction = entry.get("instruction")
        if not isinstance(alias, str) or not alias.strip():
            raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "alias")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ToolRefused(
                RefusalReason.INVALID_ARGUMENTS, call.call_id, "instruction"
            )
        cleaned = instruction.strip()
        if len(cleaned) > MAX_DELEGATION_INSTRUCTION:
            # Refused with the number rather than truncated, the same rule
            # `memory.remember` follows: half an instruction is a task nobody
            # set, and a child would carry it out anyway.
            raise ToolRefused(
                RefusalReason.INVALID_ARGUMENTS,
                call.call_id,
                f"instruction={len(cleaned)}",
            )
        children.append((alias.strip(), cleaned, _artifacts_of(call, entry)))
    wait = call.arguments.get("wait", "all")
    if wait not in WAIT_POLICIES:
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "wait")
    return tuple(children), str(wait)


def _artifacts_of(call: ToolCallBlock, entry: dict[str, Any]) -> tuple[str, ...]:
    """The files one child was named, as written.

    Shape only: whether the parent may actually read any of them is decided
    where the grants are written, because that is the only place that knows
    what this Run can reach.
    """
    named = entry.get("artifacts", [])
    if not isinstance(named, list):
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "artifacts")
    listed = cast(list[Any], named)
    if len(listed) > MAX_DELEGATED_ARTIFACTS:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"artifacts={len(listed)}"
        )
    for item in listed:
        if not isinstance(item, str) or not item.strip():
            raise ToolRefused(
                RefusalReason.INVALID_ARGUMENTS, call.call_id, "artifacts"
            )
    return tuple(str(item).strip() for item in listed)


#: The most of one file a single read may bring into the conversation. The
#: same rule `skill.load` follows and for the same reason: refused with its
#: size rather than truncated, because a model handed half a file has no way to
#: know it is reading half and will act on the half it got.
MAX_ARTIFACT_READ_BYTES = 65_536

ARTIFACT_READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "artifact.read",
        "description": (
            "Read a file that was passed to you — by the Agent that delegated "
            "this work, or by an Agent you delegated to. You can only read "
            "files somebody handed you by id; there is no shared directory and "
            "nothing to browse. If a read is refused, the file was not passed "
            "to this piece of work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "The id you were given.",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
}

ARTIFACT_READ_ARGUMENTS = frozenset({"artifact_id"})


def artifact_read_of(call: ToolCallBlock) -> str:
    """Which file `artifact.read` asked for, or a refusal.

    Shape only. Whether this Run may read it is decided against the grants,
    which is the one place that knows what was passed to this piece of work.
    """
    unknown = set(call.arguments) - ARTIFACT_READ_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    asked = call.arguments.get("artifact_id")
    if not isinstance(asked, str) or not asked.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "artifact_id")
    return asked.strip()


def schemas_for(bound: tuple[str, ...]) -> list[dict[str, Any]]:
    """Step one: what the model is told exists.

    An Agent that binds nothing advertises nothing, so a model that never heard
    of `shell.exec` cannot correctly ask for it — which is worth having even
    though step two is what enforces it.
    """
    schemas: list[dict[str, Any]] = []
    for name in bound:
        if name == "shell.exec":
            schemas.append(SHELL_EXEC_SCHEMA)
        elif name == "platform.wait":
            schemas.append(PLATFORM_WAIT_SCHEMA)
        elif name == "skill.load":
            schemas.append(SKILL_LOAD_SCHEMA)
        elif name == "skill.propose":
            schemas.append(SKILL_PROPOSE_SCHEMA)
        elif name == "memory.remember":
            schemas.append(MEMORY_REMEMBER_SCHEMA)
        elif name == "session.search":
            schemas.append(SESSION_SEARCH_SCHEMA)
        elif name == "agent.delegate":
            schemas.append(AGENT_DELEGATE_SCHEMA)
        elif name == "artifact.read":
            schemas.append(ARTIFACT_READ_SCHEMA)
        elif name in FILE_SCHEMAS:
            schemas.append(FILE_SCHEMAS[name])
    return schemas


def schemas_for_agent(
    bound: tuple[str, ...], operations: Sequence[BoundOperation] = ()
) -> list[dict[str, Any]]:
    """Everything one Agent is told it has: named tools and bound operations.

    HTTP tools are not one tool with a fixed name but a family generated by
    what a Version bound, so this is the first thing here that needs to know
    more than a list of names. It stays one function because the two callers
    must agree — the context planner measures the schema list against the
    window and the request carries it, and a round that measured one list and
    sent another would be a round whose plan was about a different request.

    `authorize` is untouched: its second check still recognizes bindings and
    never a name, and an HTTP call is never authorized here at all — it becomes
    no `SandboxCommand`, because the platform sends it.
    """
    return schemas_for(bound) + schemas_for_operations(list(operations))


def wait_seconds_of(call: ToolCallBlock) -> int:
    """How long `platform.wait` asked to sleep, or a refusal.

    Bounds only. Whether the Run is *allowed* to wait — and what that does to
    its state — is `decide_after_round`'s and `RunStateMachine`'s to answer, the
    same as every other outcome.
    """
    unknown = set(call.arguments) - PLATFORM_WAIT_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    seconds = call.arguments.get("seconds")
    # `isinstance(True, int)` is true, and a model that sent `true` did not name
    # a duration. Booleans are excluded before the range is looked at.
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "seconds")
    if not 1 <= seconds <= MAX_WAIT_SECONDS:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"seconds={seconds}"
        )
    return seconds


def skill_load_of(call: ToolCallBlock) -> SkillLoadRequest:
    """What `skill.load` asked for, or a refusal.

    Shape only. Whether the Run bound that skill, and whether the file is small
    enough to send, are answered where the Run's bindings are — the same split
    `wait_seconds_of` makes between "is this a duration" and "may this Run
    wait".

    The path is checked the way a skill package's own paths were checked when it
    was stored: no absolute path and no `..`. Nothing here opens a file, so this
    is not a traversal defence — it is the same refusal the catalog would have
    given, said at the same time as the rest of the argument errors.
    """
    unknown = set(call.arguments) - SKILL_LOAD_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    skill: object = call.arguments.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "skill")
    path = call.arguments.get("path", DEFAULT_SKILL_PATH)
    if not isinstance(path, str) or not path.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "path")
    if path.startswith("/") or ".." in path.split("/"):
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "path")
    return SkillLoadRequest(skill=skill.strip(), path=path.strip())


def skill_propose_of(call: ToolCallBlock) -> SkillProposeRequest:
    """What `skill.propose` asked for, or a refusal.

    Shape only, like `skill_load_of`. Whether the files parse as a package,
    what the scan says about them, and whether this Run has already proposed
    something are all answered where the catalog is.
    """
    unknown = set(call.arguments) - SKILL_PROPOSE_ARGUMENTS
    if unknown:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, ",".join(sorted(unknown))
        )
    sent: object = call.arguments.get("files")
    if not isinstance(sent, list) or not sent:
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "files")
    files = cast(list[object], sent)
    if len(files) > MAX_PROPOSAL_FILES:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"files={len(files)}"
        )
    collected: list[tuple[str, str]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "files")
        item = cast(dict[str, object], entry)
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "files[].path")
        if not isinstance(content, str):
            raise ToolRefused(
                RefusalReason.INVALID_ARGUMENTS, call.call_id, "files[].content"
            )
        collected.append((path.strip(), content))
    skill = call.arguments.get("skill")
    if skill is not None and (not isinstance(skill, str) or not skill.strip()):
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "skill")
    return SkillProposeRequest(
        files=tuple(collected),
        skill=None if skill is None else skill.strip(),
    )


def authorize(*, bound: tuple[str, ...], call: ToolCallBlock) -> AuthorizedCall:
    """Step two: what actually runs, against the arguments that arrived."""
    if call.name not in IMPLEMENTED_TOOLS or call.name in PLATFORM_TOOLS:
        raise ToolRefused(RefusalReason.UNKNOWN_TOOL, call.call_id, call.name)
    if call.name not in bound:
        # Nothing about the call may be wrong except this. The schema list the
        # model was handed is not a control; this is.
        raise ToolRefused(RefusalReason.NOT_AUTHORIZED, call.call_id, call.name)
    if call.name in FILE_ARGUMENTS:
        return AuthorizedCall(
            call_id=call.call_id,
            name=call.name,
            command=_file_command(call),
            changes_workspace=changes_workspace(call.name),
        )
    return AuthorizedCall(
        call_id=call.call_id,
        name=call.name,
        command=_shell_command(call),
        changes_workspace=True,
    )


def _file_command(call: ToolCallBlock) -> SandboxCommand:
    try:
        return file_tool_command(call)
    except FilePathRefused as hostile:
        # The same refusal every hostile path gets, with no shape-specific
        # detail an attacker could sort probes by.
        raise ToolRefused(
            RefusalReason.NOT_AUTHORIZED, call.call_id, "path"
        ) from hostile
    except FileArgumentsInvalid as invalid:
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, invalid.detail
        ) from invalid


def _shell_command(call: ToolCallBlock) -> SandboxCommand:
    arguments = call.arguments
    unexpected = set(arguments) - SHELL_EXEC_ARGUMENTS
    if unexpected:
        # Dropping it silently would leave a model believing it got what it
        # asked for, which is worse than being told no.
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"unexpected: {sorted(unexpected)}"
        )

    command: Any = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "command")

    cwd: Any = arguments.get("cwd", DEFAULT_WORKING_DIRECTORY)
    if not isinstance(cwd, str) or not _inside_workspace(cwd):
        raise ToolRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED, call.call_id, str(cwd))

    timeout: Any = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "timeout_seconds")
    if timeout > MAX_TIMEOUT_SECONDS:
        # Refused rather than clamped: a clamp runs a different command than
        # the model asked for and says nothing about it.
        raise ToolRefused(RefusalReason.TIMEOUT_TOO_LONG, call.call_id, str(timeout))

    return SandboxCommand(
        # The whole line goes to a shell that owns the quoting. A platform that
        # split it would own every quoting bug of every command any Agent ever
        # writes, and would get them subtly wrong.
        argv=["/bin/bash", "-lc", command],
        cwd=cwd,
        timeout_seconds=timeout,
        output_limit=DEFAULT_OUTPUT_BYTES,
    )


def _inside_workspace(cwd: str) -> bool:
    """Normalized before comparing, so `..` cannot walk out of an allowed root.

    A prefix test alone would also accept `/workspace/datax`, which is not the
    data mount and is a directory an Agent could create.
    """
    if not cwd.startswith("/"):
        return False
    parts: list[str] = []
    for piece in cwd.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return False
            parts.pop()
            continue
        parts.append(piece)
    resolved = "/" + "/".join(parts)
    return any(
        resolved == allowed or resolved.startswith(f"{allowed}/")
        for allowed in ALLOWED_WORKING_DIRECTORIES
    )

"""What a description of somebody else's API turns into, decided before use.

Product design §16.1. A document goes in; a list of operations an Agent may be
bound to comes out, or a refusal naming what is wrong with it. Pure: nothing
here opens a socket, reads a database, or resolves a name.

**JSON only.** Every OpenAPI toolchain can emit it, and a YAML parser is a
large surface to point at a document an administrator pasted in — the same
argument that made the skill manifest's frontmatter parser fifteen lines
instead of a dependency.

**Nothing is accepted that a person could not check.** A path with a traversal
in it, a parameter in a place this platform does not implement, a document with
four hundred operations: each is refused rather than partly supported, because
partly supported means a tool list nobody reviews and a request that is not the
one its author wrote.

**`$ref` is not resolved.** A reference is a second document's worth of
behaviour hiding behind a string, and resolving one means following it — which
is exactly the kind of quiet indirection an operator reviewing a tool cannot
audit. A schema containing one is kept as it is and passed to the model as it
is; what this module refuses is a reference in the places it must understand
itself, which is the method, the path and the parameter list.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

#: What one document may weigh. Large enough for a real enterprise API, small
#: enough that a paste-in cannot be a memory decision.
MAX_DOCUMENT_BYTES = 1024 * 1024

#: How many operations one registration may offer. Not arithmetic — a bound: a
#: list this long is one nobody reads, and an Agent needing more than this
#: wants two tools rather than one.
MAX_OPERATIONS = 128

#: Parameters on a single operation. Same reasoning, one level down.
MAX_PARAMETERS = 32

#: The methods this platform serves. `TRACE` and the rest are left out of a
#: document rather than refusing it: the remainder is still usable, and
#: refusing a whole API over one exotic operation pushes an administrator into
#: editing somebody else's export.
SERVED_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")

#: Methods that change nothing at the far end. §16.3 requires an approval
#: before an external write, so something has to draw this line — and it is
#: drawn by the method rather than by the operation's prose, because prose is
#: what an API author wrote and a method is what the request does.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Where a parameter may live. `header` and `cookie` are refused rather than
#: ignored: a dropped header is a request that goes out without the thing its
#: author thought it carried, and headers are where credentials live — which is
#: the platform's business and never a model's.
SERVED_LOCATIONS = frozenset({"path", "query"})

_TEMPLATE = re.compile(r"\{([^{}]*)\}")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_OPERATION_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

#: Roughly what one JSON character costs a model, reusing the context planner's
#: conservative bound rather than inventing a second one.
_CHARS_PER_TOKEN = 3


class OpenApiRefused(Exception):
    """A document this platform will not register, and why.

    Prose rather than a code: every refusal here is something the person
    holding the document can act on.
    """


@dataclass(frozen=True)
class OperationParameter:
    name: str
    location: Literal["path", "query"]
    required: bool
    schema: dict[str, Any]
    description: str | None = None


@dataclass(frozen=True)
class Operation:
    """One call an Agent may be bound to."""

    operation_id: str
    method: str
    path: str
    summary: str | None
    parameters: tuple[OperationParameter, ...]
    #: The `application/json` request body schema, when the operation takes
    #: one. Other media types are not offered: a model produces JSON, and a
    #: tool that accepted form encoding would be a tool whose arguments the
    #: platform has to guess the shape of.
    body_schema: dict[str, Any] | None = None

    @property
    def read_only(self) -> bool:
        return self.method in READ_ONLY_METHODS


@dataclass(frozen=True)
class OpenApiDocument:
    """A parsed document, with the identity a binding is written against."""

    title: str
    version: str
    operations: tuple[Operation, ...]
    #: Over the normalized JSON, so two spellings of one document are one
    #: version. A binding names this, never a URL.
    content_hash: str


def parse_document(body: str) -> OpenApiDocument:
    """One comparable document, or a named refusal."""
    size = len(body.encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise OpenApiRefused(
            f"the document is {size} bytes; an OpenAPI document may be "
            f"{MAX_DOCUMENT_BYTES}"
        )
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError as error:
        raise OpenApiRefused(
            f"the document is not JSON: {error.msg} at line {error.lineno}. "
            "Export it as JSON — this platform does not parse YAML."
        ) from error
    if not isinstance(loaded, dict):
        raise OpenApiRefused("an OpenAPI document is an object")
    document = cast(dict[str, Any], loaded)
    if not isinstance(document.get("openapi"), str):
        raise OpenApiRefused("the document declares no `openapi` version")
    info = document.get("info")
    if not isinstance(info, dict):
        raise OpenApiRefused("the document has no `info` block")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise OpenApiRefused("the document declares no `paths`")

    operations = _operations(cast(dict[str, Any], paths))
    if len(operations) > MAX_OPERATIONS:
        raise OpenApiRefused(
            f"the document offers {len(operations)} operations; at most "
            f"{MAX_OPERATIONS} may be registered at once"
        )
    if not operations:
        raise OpenApiRefused("the document offers no operation this platform can call")

    details = cast(dict[str, Any], info)
    return OpenApiDocument(
        title=str(details.get("title", "")),
        version=str(details.get("version", "")),
        operations=tuple(sorted(operations, key=lambda item: item.operation_id)),
        content_hash=_content_hash(document),
    )


def estimated_schema_tokens(operations: list[Operation]) -> int:
    """What telling a model about these operations costs, as an upper bound.

    One function, used by the publish-time budget check and by the console, so
    the two can never disagree about how big a binding is. An estimate and not
    a measurement, the same admission `estimate_tokens` makes: it decides what
    to send and never what to bill.
    """
    if not operations:
        return 0
    total = 0
    for operation in operations:
        described = {
            "name": operation.operation_id,
            "summary": operation.summary,
            "parameters": [
                {
                    "name": parameter.name,
                    "in": parameter.location,
                    "required": parameter.required,
                    "schema": parameter.schema,
                    "description": parameter.description,
                }
                for parameter in operation.parameters
            ],
            "body": operation.body_schema,
        }
        text = json.dumps(described, ensure_ascii=False, sort_keys=True)
        total += -(-len(text) // _CHARS_PER_TOKEN)
    return total


def _operations(paths: dict[str, Any]) -> list[Operation]:
    found: list[Operation] = []
    seen: set[str] = set()
    for raw_path, raw_item in sorted(paths.items()):
        path = _checked_path(str(raw_path))
        if not isinstance(raw_item, dict):
            raise OpenApiRefused(f"{path}: a path item is an object")
        item = cast(dict[str, Any], raw_item)
        for method in SERVED_METHODS:
            body = item.get(method)
            if not isinstance(body, dict):
                continue
            operation = _operation(path, method, cast(dict[str, Any], body))
            if operation.operation_id in seen:
                # The id is the key a binding is written against, so two of
                # them is a document in which a binding is ambiguous.
                raise OpenApiRefused(
                    f"two operations share the id {operation.operation_id!r}"
                )
            seen.add(operation.operation_id)
            found.append(operation)
    return found


def _operation(path: str, method: str, body: dict[str, Any]) -> Operation:
    operation_id = body.get("operationId")
    if not isinstance(operation_id, str) or not _OPERATION_ID.match(operation_id):
        # Not generated from the path: a generated id moves when the document
        # is reordered, and a published Agent would quietly start calling
        # something else.
        raise OpenApiRefused(
            f"{method.upper()} {path} has no usable operationId; "
            "this platform binds operations by that name"
        )
    parameters = _parameters(path, operation_id, body.get("parameters"))
    _check_template(path, operation_id, parameters)
    summary = body.get("summary") or body.get("description")
    return Operation(
        operation_id=operation_id,
        method=method.upper(),
        path=path,
        summary=str(summary) if isinstance(summary, str) else None,
        parameters=parameters,
        body_schema=_body_schema(operation_id, body.get("requestBody")),
    )


def _parameters(
    path: str, operation_id: str, raw: object
) -> tuple[OperationParameter, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise OpenApiRefused(f"{operation_id}: `parameters` is a list")
    entries = cast(list[object], raw)
    if len(entries) > MAX_PARAMETERS:
        raise OpenApiRefused(
            f"{operation_id} takes {len(entries)} parameters; at most {MAX_PARAMETERS}"
        )
    found: list[OperationParameter] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OpenApiRefused(f"{operation_id}: a parameter is an object")
        parameter = cast(dict[str, Any], entry)
        if "$ref" in parameter:
            raise OpenApiRefused(
                f"{operation_id}: a parameter given as $ref cannot be checked here; "
                "inline it before registering the document"
            )
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not name.strip():
            raise OpenApiRefused(f"{operation_id}: a parameter has no name")
        if location not in SERVED_LOCATIONS:
            raise OpenApiRefused(
                f"{operation_id}: parameter {name!r} is in {location!r}; this "
                "platform sends path and query parameters only, and a header "
                "silently dropped is a request nobody wrote"
            )
        schema = parameter.get("schema")
        found.append(
            OperationParameter(
                name=name.strip(),
                location=cast(Literal["path", "query"], location),
                required=bool(parameter.get("required", location == "path")),
                schema=cast(dict[str, Any], schema)
                if isinstance(schema, dict)
                else {"type": "string"},
                description=(
                    str(parameter["description"])
                    if isinstance(parameter.get("description"), str)
                    else None
                ),
            )
        )
    del path
    return tuple(found)


def _body_schema(operation_id: str, raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpenApiRefused(f"{operation_id}: `requestBody` is an object")
    content = cast(dict[str, Any], raw).get("content")
    if not isinstance(content, dict):
        return None
    entry = cast(dict[str, Any], content).get("application/json")
    if not isinstance(entry, dict):
        # Another media type is not a refusal, only a body this platform will
        # not compose: a model produces JSON, and guessing at form encoding
        # would be guessing at what its arguments meant.
        return None
    schema = cast(dict[str, Any], entry).get("schema")
    return cast(dict[str, Any], schema) if isinstance(schema, dict) else None


def _checked_path(path: str) -> str:
    if not path.startswith("/"):
        raise OpenApiRefused(f"{path!r}: a path starts at the root")
    if "//" in path or ".." in path.split("/"):
        raise OpenApiRefused(
            f"{path!r}: a path with an empty or traversing segment is not one "
            "this platform will build a request from"
        )
    for variable in _TEMPLATE.findall(path):
        if not _VARIABLE.match(variable):
            raise OpenApiRefused(
                f"{path!r}: {variable!r} is not a template variable a caller "
                "could fill"
            )
    return path


def _check_template(
    path: str, operation_id: str, parameters: tuple[OperationParameter, ...]
) -> None:
    """The template's variables and the path parameters have to be the same set.

    Either direction is a request that is not the one the model asked for: a
    parameter nothing fills leaves `{orderId}` in the URL, and a value with no
    place to go is silently discarded.
    """
    named = {variable for variable in _TEMPLATE.findall(path)}
    supplied = {
        parameter.name for parameter in parameters if parameter.location == "path"
    }
    missing = named - supplied
    extra = supplied - named
    if missing:
        raise OpenApiRefused(
            f"{operation_id}: {sorted(missing)} appear in the path and no "
            "parameter fills them"
        )
    if extra:
        raise OpenApiRefused(
            f"{operation_id}: {sorted(extra)} are path parameters the path "
            "never names"
        )


def _content_hash(document: dict[str, Any]) -> str:
    """The identity of a document's content, independent of its spacing.

    Sorted and separator-free, the same normalization the skill package hash
    uses — a binding names a version, and two spellings of one document must
    not be two versions.
    """
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

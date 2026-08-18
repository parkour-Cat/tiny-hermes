"""One operation, as a row keeps it and as it comes back.

Its own module because two stores read this encoding: the catalog writes it,
and a Run's `ExecutionContext` reads it to assemble the operations a round may
call. A second spelling of it would be a second place for the two to disagree
about what an operation is.

Stored rather than re-parsed on every read. A Run assembling its tool list
should not depend on a document parsing the same way today as it did when
somebody reviewed it.
"""

from typing import Any, cast

from tiny_hermes.tools.domain.openapi import Operation, OperationParameter


def operation_document(operation: Operation) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path,
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
        "body_schema": operation.body_schema,
    }


def operation_from_document(entry: dict[str, Any]) -> Operation:
    parameters = cast(list[dict[str, Any]], entry.get("parameters") or [])
    return Operation(
        operation_id=str(entry["operation_id"]),
        method=str(entry["method"]),
        path=str(entry["path"]),
        summary=entry.get("summary"),
        parameters=tuple(
            OperationParameter(
                name=str(parameter["name"]),
                location=parameter["in"],
                required=bool(parameter["required"]),
                schema=parameter.get("schema") or {"type": "string"},
                description=parameter.get("description"),
            )
            for parameter in parameters
        ),
        body_schema=entry.get("body_schema"),
    )

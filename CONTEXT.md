# tiny-hermes Runtime

tiny-hermes Runtime defines the business language for configuring agents and accepting, ordering, controlling, and observing their work inside a workspace.

## Agent configuration

**Agent**:
A stable workspace-owned identity whose published behavior can evolve over time.
_Avoid_: Bot, assistant instance

**Agent Draft**:
The single mutable candidate configuration associated with an Agent.
_Avoid_: Unpublished Agent, editable version

**Agent Version**:
An immutable, numbered snapshot of an Agent Draft that a Run can reference permanently.
_Avoid_: Draft version, live config

## Conversation and execution

**Session**:
An ordered conversation and work context for one Agent and one calling subject.
_Avoid_: Thread, chat room

**Run**:
One accepted unit of Agent work inside a Session, with its own lifecycle, events, limits, and outcome.
_Avoid_: Task, job, message

**Head Run**:
The earliest non-terminal Run in a Session and the only Run in that Session eligible for execution.
_Avoid_: Active Run, current message

**Pending Run**:
A non-terminal Run ordered after the Head Run and therefore not yet eligible for execution.
_Avoid_: Background Run, parallel Run

**Checkpoint Step**:
A completed piece of Agent work whose result and replay safety are known and saved together.
_Avoid_: Time slice, transaction

**Execution Slice**:
A bounded period during which one Worker may advance a Run before returning it to the queue at a safe checkpoint.
_Avoid_: Checkpoint, model call

## Accounting and history

**Run Event**:
An immutable, ordered fact describing something that happened to one Run.
_Avoid_: Log line, audit event

**Run Budget Scope**:
The shared limits and consumption counters owned by a root Run and all Runs derived from its retries.
_Avoid_: Per-Run quota, billing account

**Derived Retry**:
A new Run that resumes from the last replay-safe checkpoint of a failed Run while sharing the original Run Budget Scope.
_Avoid_: Resume, rerun, reset

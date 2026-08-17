# M2A Goal Loop and Context Budget Design

> Date: 2026-08-17
>
> Status: written design for user review
>
> Delivery slice: M2 phase A, itself split into 2A-1 and 2A-2

## 1. Purpose and authority

0.1 ships a Run that executes as many model and tool rounds as it needs and then
stops when the provider says it is finished. That last clause is the whole of the
subject here. `runs/domain/slice_policy.py` reads:

```python
if outcome.stop_reason is StopReason.COMPLETED:
    return SliceDecision(RunSignal.COMPLETED)
```

A Run therefore ends when a model asserts that it ended. Product design §531 says
the opposite: 模型的判断只是建议，服务端必须验证状态、预算和权限. This slice makes
that true, and then makes the resulting longer conversations survive a context
window.

Authoritative inputs:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4, §12
  (Goal loop), §7.4.2 (context budget and trimming order), §17.3 (recovery
  boundary), §27.2.1 and §27.2.7 (the two acceptance scenarios this slice owns).
- `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §4.
- The 0.1 phase 2B/2C/3A designs for the seams consumed here.

## 2. Observable outcome

After phase A, a workspace developer can publish an Agent that carries a
completion condition, and:

1. A task needing several tool rounds ends because the platform checked the
   condition, not because the provider set a stop reason.
2. A model that claims completion while its declared verification command fails
   does not end the Run; the loop receives an instruction naming what is still
   unmet and continues.
3. A Run that asks to wait enters `waiting_external` with a deadline, gives up
   its WorkerLease and its sandbox, and is woken by the Scheduler.
4. Reaching the round ceiling produces `paused(limit)` and a `run_limit_reached`
   event; widening the budget resumes without resetting any consumed total.
5. A conversation that outgrows its endpoint window is trimmed in the fixed order
   and then structurally compacted, with the covered message range and original
   references recorded.
6. A conversation whose incompressible content still does not fit enters
   `paused(context_overflow)` — no silent deletion, no provider call.

## 3. What 0.1 actually leaves at this seam

Verified in the tree at `aa3f714`, not inferred from the specs.

| Fact | Where | Consequence for this slice |
|---|---|---|
| The slice loop is already multi-round: `while True`, tool calls answered in-loop | `runs/application/worker.py:254` | The Goal loop is not a new loop. It changes one exit condition. |
| `COMPLETED` from the provider ends the Run | `runs/domain/slice_policy.py:52` | The single line this slice displaces. |
| `waiting_external` and `wait_kind` exist; nothing produces them | `runs/domain/models.py:14`, `runs/application/scheduler.py:281` | `wait` is wiring, not new state. |
| `AgentLimits.max_model_calls` defaults to 20 and is capped at 20 | `agents/domain/models.py:16` | The round valve exists. Its ceiling was set when nothing could run long. |
| `BudgetSummary.allows_execution()` already refuses on calls, time, elapsed and tokens | `runs/domain/models.py:409` | `paused(limit)` needs no new arithmetic. |
| `ModelEndpointSpec` declares `context_window`, `max_output_tokens`, `usage_quality` | `model_catalog/domain/models.py:63` | The window is known. `context_accounting` is the one missing field. |
| `ModelRequest` carries `messages`, `personality`, `tools`, `round_index` | `runs/ports/model.py:47` | The prompt is assembled at `worker.py:1221` with no budget of any kind. |
| `AgentSpec` is frozen, content-hashed, `schema_version: 1` | `agents/domain/models.py:85` | Adding optional fields is a widening; published versions keep their hashes. Removing or narrowing is not. |

## 4. Decisions

### 4.1 The judge is server-side code, and the model's stop reason is one input

§12.1 says the judge returns `done` / `continue` / `wait` after each Agent round.
It does not say the judge is a model. Making it a second model call would double
the cost of every round and would put the verification of a model's claim in the
hands of a model.

So: `runs/domain/goal.py` holds a pure function over (a) the model's proposal,
(b) the declared completion condition, (c) the results of any verification the
platform ran, (d) budget and control state. It performs no I/O and is unit
testable in the same way `slice_policy.decide_after_round` is. The Worker
supplies the facts and applies the verdict.

### 4.2 A Run with no declared completion condition behaves exactly as it does today

`AgentSpec` is content-hashed and every published version must keep its hash.
The completion condition is therefore an optional field, and when it is absent
the judge returns `done` on `StopReason.COMPLETED` — 0.1's behaviour, reached
through the new code path. Existing published Agents do not change, and the
acceptance for this slice is written against Agents that do declare one.

This also means the slice can land without a data migration of `agent_versions`.

### 4.3 The completion condition is declared, not inferred

Per §12.2, on the AgentVersion:

- `expected_artifacts`: paths under `/workspace/data` that must exist.
- `verification_command`: one command, run in the sandbox. Never on the host —
  the existing `command.run` execution path is reused, so the host-fallback ban
  proven in 0.1 covers it unchanged.
- `constraints`: free text handed to the model, not machine-checked. Recorded so
  a reader can see what the Agent was told.
- `stop_conditions`: machine-checked ceilings owned by the judge (max rounds for
  this Agent, within the platform ceiling).

`expected_artifacts` and `verification_command` are the two the platform can
actually check, and they are the two the acceptance uses. `constraints` is
deliberately declared as unenforced so nobody reads it as a guarantee.

### 4.4 Verification runs at most once per claimed completion, and its failure is not the Run's failure

A verification command that exits nonzero means the model's `done` was wrong, not
that the task failed. The verdict becomes `continue` with an instruction naming
the failed check. A verification command that cannot be executed at all (no
sandbox, controller refusal) is different: the platform cannot confirm or deny,
so the Run goes to `paused(operator)` rather than silently accepting or
rejecting. §17.3's rule — do not continue past an outcome you could not observe.

### 4.5 `continue` appends a platform-authored instruction to the conversation

§12.1: continue 生成下一轮指令. The instruction is a `CanonicalMessage` with
`role="user"` — the only role the kernel has for "what the agent is being asked"
— carrying a platform marker in the checkpoint so a transcript reader can tell it
from something a human typed. It names the unmet conditions and nothing else; it
does not restate the task, because the task is already in the conversation.

### 4.6 `wait` ships with one producer: a timer

`waiting_external` needs a producer or the Scheduler's wait scan stays untested.
M2A implements `wait_kind="timer"`: the judge returns `wait` with a duration, the
Run enters `waiting_external` with `wait_deadline_at`, releases its lease and
destroys its sandbox, and the Scheduler re-queues it at the deadline. Overrunning
the deadline without a wake is `paused(external_timeout)`, which is the behaviour
§13.10 already specifies for the child-run case.

`wait_kind="approval"` arrives in M2C and `wait_kind="child_runs"` in M2E. They
reuse this path rather than adding one.

### 4.7 The round ceiling moves from a constant to a platform setting

`AgentLimits.max_model_calls` is `ge=1, le=20`. That ceiling was chosen when a
Run could not run long; a Goal loop that must not exceed 20 model calls cannot do
much. §12.3 gives platform administrators the default and the maximum, so the
hard `le=20` becomes a platform setting with 20 as its default, and `AgentLimits`
is validated against the live ceiling rather than against a literal.

Raising a ceiling is a widening for the schema and a narrowing for nobody: no
published spec becomes invalid.

### 4.8 Context budget is computed per request, and only what is sent is charged

The segment table in §7.4.2 is instance-default configuration, not a product
constant, and the design says so. It lives in platform settings with the same
shape as the table, overridable per AgentVersion within the platform's hard caps.

Counting: `usage_quality` on the endpoint says whether the provider reports
tokens, and 0.1 already refuses to invent them (`UsageQuality` has no
`estimated`, by an explicit decision recorded in `runs/ports/model.py:38`). The
budget planner needs a count *before* the call, which the provider cannot give.
So the planner uses a declared local tokenizer per endpoint, and its output is
labelled a plan estimate rather than usage: it decides what to send, never what
to bill. Billing still comes from the response, unchanged. An endpoint with no
verified tokenizer gets a conservative character-based bound — enough to trim
safely, never reported as a token count.

This keeps §12.4's rule intact: unknown usage is not zero, and it is also not
allowed to become a fake number by way of the planner.

### 4.9 Compaction is a recorded transformation, never a deletion

A compaction writes a summary message plus the covered `(first_sequence,
last_sequence)` range and the original message ids. The originals stay in
`session_messages`. If compaction fails, the originals are used; if the originals
do not fit, the Run pauses. There is no branch in which a message becomes
unreachable.

## 5. Schema and API changes

Additive only.

- `AgentSpec.completion: CompletionCondition | None = None` — optional, so
  `schema_version` stays 1 and existing content hashes are unchanged. Pinned by a
  test, as the `tools` widening was.
- `AgentSpec.context_budget: ContextBudget | None = None` — same reasoning.
- `ModelEndpointSpec.context_accounting: Literal["shared", "separate"]` with a
  default, plus an optional `tokenizer` name.
- New `RunEventType`s: `goal_verdict`, `context_trimmed`, `context_compacted`.
- New `PauseReason` use: `context_overflow` (the enum member already exists).
- `RunSnapshot` gains the current round index and the last Goal verdict, so the
  console can show why a Run is still running.
- Publish API gains the `context_budget_unsatisfied` refusal with per-segment
  scaling advice.

No column is dropped, no enum member is removed, and no published AgentVersion is
migrated.

## 6. Slices

### 2A-1 — the Goal loop

Everything in §4.1 through §4.7. Ends with: a declared-completion Agent that
loops until the platform is satisfied, waits on a timer, and pauses at the round
ceiling.

Does not touch the prompt builder. Conversations get longer in this slice, and
the endpoint window is not yet defended — which is precisely why 2A-2 follows
immediately and why the roadmap keeps them in one phase.

### 2A-2 — context budget, trimming and compaction

Everything in §4.8 and §4.9, plus the publish-time `context_budget_unsatisfied`
check and the console's context and compaction events.

## 7. What phase A does not do

Sub-agents, memory, skills, MCP or HTTP tools, `egress-proxy`, monetary cost
ceilings, and approvals. Each is a later phase in the roadmap. In particular the
"记忆" segment of the budget table is allocated and always empty in this slice —
the trimming order names it, and there is nothing there to trim until M2D.

## 8. Acceptance mapping

| Product §27.2 | Slice | How it is checked |
|---|---|---|
| 1. Goal judges done/continue/wait; safety valve → recoverable pause | 2A-1 | Integration: a three-round task; a false `done` refused by a failing verification; a timer wait woken by the Scheduler; the round ceiling reached and resumed |
| 7. Compaction keeps original references; failure → `paused(context_overflow)` | 2A-2 | Integration: a conversation forced past a small window; originals retrievable after compaction; an incompressible input pausing rather than truncating |

Both also get unit coverage at the pure-function boundary (`goal.py`, the budget
planner), which is where the interesting cases are cheap.

## 9. Risks

- **The judge becomes a second place that decides Run state.** It must not.
  `RunStateMachine` stays the only authority; the judge returns a verdict and the
  slice policy translates it into a signal, exactly as `decide_after_round` does
  now.
- **A `continue` loop that never converges.** The round ceiling is the backstop
  and it is why §4.7 is in this slice rather than deferred.
- **Tokenizer drift.** A planner estimate that is too low sends a request the
  endpoint rejects. The planner reserves headroom and the provider's own error is
  handled as a failed round, not as a crash; an endpoint without a verified
  tokenizer gets the conservative bound in §4.8.

import type { RunEventFrame, RunResponse } from "../api/types";
import type { MessageKey } from "../i18n/zh-CN";

type Situation = Pick<RunResponse, "status" | "pause_reason" | "wait_kind">;

/**
 * What a status means for this Run, when the word alone does not say.
 *
 * `waiting_external` and `paused` are each one word covering situations that
 * call for different things from the person reading them: a timer will wake
 * itself and wants nothing, an exhausted budget will not move until someone
 * widens it. Rendering both as a generic "waiting" leaves the reader unable to
 * tell whether they are the one holding it up.
 */
export function statusNote(run: Situation): MessageKey | null {
  if (run.status === "waiting_external") {
    if (run.wait_kind === "timer") {
      return "waitingTimerNote";
    }
    // Its own sentence rather than the generic one, because this is the wait
    // whose reader most needs to be told nothing is expected of them: the
    // children are listed right below, they are running, and clicking one is
    // the only useful thing to do.
    if (run.wait_kind === "child_runs") {
      return "waitingChildRunsNote";
    }
    return "waitingExternalNote";
  }
  if (run.status === "paused" && run.pause_reason === "limit") {
    return "pausedLimitNote";
  }
  if (run.status === "paused" && run.pause_reason === "context_overflow") {
    return "pausedContextOverflowNote";
  }
  return null;
}

const OUTCOMES: Record<string, MessageKey> = {
  done: "goalOutcomeDone",
  continue: "goalOutcomeContinue",
  wait: "goalOutcomeWait",
  failed: "goalOutcomeFailed",
  undecidable: "goalOutcomeUndecidable",
};

/** The judge's answer in words, or null for one this console does not know. */
export function outcomeLabel(outcome: string | null): MessageKey | null {
  return outcome === null ? null : (OUTCOMES[outcome] ?? null);
}

/** A sentence for a timeline entry, and the numbers to put in it. */
export type EventNote = { key: MessageKey; values: Record<string, string> };

const TRIMMED: Record<string, MessageKey> = {
  old_tool_results: "contextTrimmedOldToolResults",
  skill_summaries: "contextTrimmedSkillSummaries",
  memory: "contextTrimmedMemory",
};

/**
 * The numbers a note needs, or null if the payload does not carry them all.
 *
 * A sentence with a hole in it reads like a bug in the platform rather than a
 * fact about the Run, so a payload this console does not fully understand gets
 * no sentence at all — the raw payload is on the entry either way.
 */
function filled(
  payload: Record<string, unknown>,
  fields: Record<string, string>,
): Record<string, string> | null {
  const values: Record<string, string> = {};
  for (const [placeholder, field] of Object.entries(fields)) {
    const value = payload[field];
    if (typeof value !== "number") {
      return null;
    }
    values[placeholder] = String(value);
  }
  return values;
}

/**
 * The words a note needs, with an em dash where the server recorded nothing.
 *
 * Unlike `filled`, a missing one of these does not cost the reader the whole
 * sentence: a summary call routed to the deterministic stand-in names no
 * endpoint and no model, and "a model wrote this, and the platform did not
 * record which" is still the fact that matters most on that entry. Same
 * stand-in as `httpRefusedNote`'s missing operation.
 */
function text(
  payload: Record<string, unknown>,
  fields: Record<string, string>,
): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [placeholder, field] of Object.entries(fields)) {
    const value = payload[field];
    values[placeholder] = typeof value === "string" && value !== "" ? value : "—";
  }
  return values;
}

/**
 * What the platform did to the context before a round, said in words.
 *
 * `context_trimmed` and `context_compacted` are the two events that report a
 * decision nobody asked for: the conversation was too large for the window, so
 * the round was sent something other than what the transcript holds. An event
 * name and a JSON blob leave a reader guessing whether anything was lost —
 * nothing is, and that is the part worth writing out.
 *
 * Every number here is a plan estimate, and the messages say so. None of them
 * is usage, and none of them is billed.
 */
export function eventNote(frame: Pick<RunEventFrame, "event_type" | "payload">): EventNote | null {
  if (frame.event_type === "context_trimmed") {
    const key = TRIMMED[String(frame.payload.segment)];
    const values = filled(frame.payload, { dropped: "dropped", freed: "freed_estimate" });
    return key === undefined || values === null ? null : { key, values };
  }
  if (frame.event_type === "skill_loaded") {
    // Said in words rather than left as a payload, because this is the one
    // entry that explains where text in the transcript came from — and the
    // sentence carries the boundary the prompt itself carries: a skill is a
    // workspace's reference material, not the platform speaking.
    const skill = frame.payload.skill;
    const path = frame.payload.path;
    const values = filled(frame.payload, { bytes: "bytes" });
    if (typeof skill !== "string" || typeof path !== "string" || values === null) {
      return null;
    }
    return { key: "skillLoadedNote", values: { ...values, skill, path } };
  }
  if (frame.event_type === "skill_proposed") {
    // No numbers to fill: what matters is that nothing changed, which is true
    // of every proposal regardless of what is in it.
    return { key: "skillProposedNote", values: {} };
  }
  if (frame.event_type === "tool_schema_budget_exceeded") {
    // Both numbers, because a refusal an author can act on is one with the
    // numbers in it — and because the fix is either fewer tools or a larger
    // segment, and neither is obvious without them.
    const values = filled(frame.payload, { estimate: "estimate", allowance: "allowance" });
    return values === null ? null : { key: "toolBudgetNote", values };
  }
  if (frame.event_type === "mcp_tools_revalidated") {
    // What came back short. A Run that quietly had fewer tools than its
    // Version bound is one whose behaviour changed with nobody publishing
    // anything, so this is said rather than left in a payload.
    const unreachable = frame.payload.unreachable;
    const missing = frame.payload.missing;
    if (!Array.isArray(unreachable) || !Array.isArray(missing)) {
      return null;
    }
    return {
      key: "mcpRevalidatedNote",
      values: {
        unreachable: unreachable.join(", ") || "—",
        missing: missing.join(", ") || "—",
      },
    };
  }
  if (frame.event_type === "http_call_refused") {
    const reason = frame.payload.reason;
    const tool = frame.payload.tool;
    const operation = frame.payload.operation;
    if (typeof reason !== "string" || typeof tool !== "string") {
      return null;
    }
    return {
      key: "httpRefusedNote",
      values: {
        tool,
        operation: typeof operation === "string" ? operation : "—",
        reason,
      },
    };
  }
  if (frame.event_type === "run_delegated") {
    // Who and how many, because the wait that follows is about exactly this
    // set — and `all` versus `any` decides whether the reader should expect
    // the siblings to finish or to be cancelled.
    const children = frame.payload.children;
    const wait = frame.payload.wait;
    if (!Array.isArray(children) || typeof wait !== "string") {
      return null;
    }
    return {
      key: wait === "any" ? "delegatedAnyNote" : "delegatedAllNote",
      values: { count: String(children.length) },
    };
  }
  if (frame.event_type === "run_approval_requested") {
    // The one event where "who is waiting" matters more than what happened:
    // a Run in `waiting_approval` resumes when a person acts and never on its
    // own, so the sentence has to send the reader to a person.
    return { key: "approvalRequestedNote", values: {} };
  }
  if (frame.event_type === "run_approval_approved") {
    return { key: "approvalApprovedNote", values: {} };
  }
  if (frame.event_type === "context_compacted") {
    const values = filled(frame.payload, {
      first: "first_sequence",
      last: "last_sequence",
      covered: "covered",
      freed: "freed_estimate",
    });
    if (values === null) {
      return null;
    }
    // Which of the two the model actually read. §7.4.2 records `source` for
    // this one reader: a semantic summary and a list saying "38 messages were
    // here" are not the same thing to answer from, and an operator holding a
    // wrong answer needs to know which one the round was given before they can
    // decide whether the summarizer is a suspect. One sentence asserting
    // "generated by fixed rules, no extra model call" over both sends them
    // away from it on exactly the rounds where it is.
    if (frame.payload.source === "model") {
      return {
        key: "contextCompactedModelNote",
        values: { ...values, ...text(frame.payload, { model: "model", endpoint: "endpoint_id" }) },
      };
    }
    if (frame.payload.source === "structural") {
      return { key: "contextCompactedStructuralNote", values };
    }
    // No `source`, or one this build has never heard of. Every compaction
    // written before §7.4.2 is in the first case and was structural in fact,
    // but the event does not say so, and a console that fills that in is
    // asserting on the reader's behalf the very thing the field was added to
    // stop being assumed.
    return { key: "contextCompactedNote", values };
  }
  return null;
}

/** Puts a note's numbers into its translated sentence. */
export function fill(said: string, values: Record<string, string>): string {
  return Object.entries(values).reduce(
    (text, [placeholder, value]) => text.replace(`{${placeholder}}`, value),
    said,
  );
}

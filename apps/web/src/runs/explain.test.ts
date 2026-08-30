import { expect, test } from "vitest";

import { enUS } from "../i18n/en-US";
import { t } from "../i18n/zh-CN";
import { eventNote, fill, outcomeLabel, statusNote } from "./explain";

function situation(
  status: string,
  { pause = null, wait = null }: { pause?: string | null; wait?: string | null } = {},
) {
  return {
    status,
    pause_reason: pause,
    wait_kind: wait,
  } as Parameters<typeof statusNote>[0];
}

test("a run waiting on its own timer is told to wait for nobody", () => {
  expect(statusNote(situation("waiting_external", { wait: "timer" }))).toBe("waitingTimerNote");
});

test("a run waiting on something outside gets the other note", () => {
  // The two share a status word and call for opposite things from the reader,
  // which is the whole reason the note exists.
  expect(statusNote(situation("waiting_external", { wait: "approval" }))).toBe(
    "waitingExternalNote",
  );
  expect(statusNote(situation("waiting_external"))).toBe("waitingExternalNote");
});

test("only an exhausted budget explains a pause", () => {
  expect(statusNote(situation("paused", { pause: "limit" }))).toBe("pausedLimitNote");
  // An operator pause was somebody's decision; the console has nothing to add.
  expect(statusNote(situation("paused", { pause: "operator" }))).toBeNull();
});

test("a run that overflowed the window says nothing was sent", () => {
  // The one pause reason a reader cannot act on without being told what it
  // means: no request was made, so the timeline shows no round to look at.
  expect(statusNote(situation("paused", { pause: "context_overflow" }))).toBe(
    "pausedContextOverflowNote",
  );
});

test("a run that is simply working needs no note", () => {
  expect(statusNote(situation("running"))).toBeNull();
  expect(statusNote(situation("completed"))).toBeNull();
});

test("every verdict the judge can return has a word", () => {
  for (const outcome of ["done", "continue", "wait", "failed", "undecidable"]) {
    expect(outcomeLabel(outcome)).not.toBeNull();
  }
});

test("a verdict this console has never heard of is not translated", () => {
  // Null rather than a guess: the page falls back to the raw value, which is
  // still the reason the Run is where it is.
  expect(outcomeLabel("something_newer")).toBeNull();
  expect(outcomeLabel(null)).toBeNull();
});

function frame(event_type: string, payload: Record<string, unknown>) {
  return { event_type, payload };
}

const TRIM = { segment: "old_tool_results", dropped: 2, freed_estimate: 9_000, references: ["c1"] };
const COMPACTION = {
  first_sequence: 1,
  last_sequence: 6,
  covered: 6,
  message_ids: ["one", "two"],
  freed_estimate: 7_400,
};

test("a trimmed tool result is explained with its count and what it freed", () => {
  const said = eventNote(frame("context_trimmed", TRIM));
  expect(said).not.toBeNull();
  const sentence = fill(t(said?.key ?? "appName"), said?.values ?? {});
  expect(sentence).toContain("2");
  expect(sentence).toContain("9000");
  // Both halves of the fact: the numbers are estimates, and nothing was lost.
  expect(sentence).toContain("估算");
  expect(sentence).toContain("会话记录");
});

test("a compaction says which messages it stood in for", () => {
  const said = eventNote(frame("context_compacted", COMPACTION));
  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("1");
  expect(sentence).toContain("6");
  expect(sentence).toContain("7400");
  expect(sentence).not.toContain("{");
});

// The three shapes `source` can arrive in. §7.4.2 added the field for one
// reason: an operator looking at a compacted session has to be able to tell
// whether the model read a semantic summary or a list saying "38 messages were
// here". A sentence that asserts one of the two for every compaction is worse
// than no sentence — it sends the reader away from the summarizer on exactly
// the rounds where the summarizer is the suspect.

test("a model-written summary names the model and the endpoint that wrote it", () => {
  const said = eventNote(
    frame("context_compacted", {
      ...COMPACTION,
      source: "model",
      endpoint_id: "6f2f2a1e-0b6d-4a1a-9b7b-7d0a2b7f9c31",
      model: "acme-large",
    }),
  );

  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("acme-large");
  expect(sentence).toContain("6f2f2a1e-0b6d-4a1a-9b7b-7d0a2b7f9c31");
  // The sentence the operator would otherwise read straight above a payload
  // that contradicts it.
  expect(sentence).not.toContain("no extra model call");
  expect(fill(t(said?.key ?? "appName"), said?.values ?? {})).not.toContain("没有为此多调");
});

test("a model-written summary whose endpoint went unrecorded still says a model wrote it", () => {
  // `endpoint_id`/`model` are null whenever the summary call went to the
  // deterministic stand-in, which names no endpoint. That is not a reason to
  // fall back to the structural sentence: a model still wrote this text.
  const said = eventNote(
    frame("context_compacted", { ...COMPACTION, source: "model", endpoint_id: null, model: null }),
  );

  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).not.toContain("no extra model call");
  expect(sentence).toContain("—");
  expect(sentence).not.toContain("{");
});

test("a structural summary is the one that may say no model was called", () => {
  const said = eventNote(frame("context_compacted", { ...COMPACTION, source: "structural" }));

  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("no extra model call");
  expect(fill(t(said?.key ?? "appName"), said?.values ?? {})).toContain("没有为此多调");
});

test("a compaction that does not say who wrote it claims neither", () => {
  // Events written before §7.4.2 carry no `source`. Guessing "structural"
  // because that is all the old build could do would be this console asserting
  // something the event does not say.
  const said = eventNote(frame("context_compacted", COMPACTION));

  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).not.toContain("no extra model call");
  expect(said?.key).not.toBe(
    eventNote(frame("context_compacted", { ...COMPACTION, source: "model" }))?.key,
  );
  expect(said?.key).not.toBe(
    eventNote(frame("context_compacted", { ...COMPACTION, source: "structural" }))?.key,
  );
});

// v2.8: a summarization call bills real money and a real slot on this Run's
// call budget, and `context_summary_billed` is the only place on the
// timeline that says so — a reader watching `consumed_model_calls` or
// `consumed_cost` move needs to land here, not on a raw payload.
const SUMMARY_BILLED = {
  endpoint_id: "6f2f2a1e-0b6d-4a1a-9b7b-7d0a2b7f9c31",
  model: "acme-large",
  input_tokens: 500,
  output_tokens: 50,
  tokens: 550,
  cost: "0.010500",
  cost_currency: "USD",
  cost_quality: "provider",
};

test("a billed summary call names its tokens, cost, and who answered", () => {
  const said = eventNote(frame("context_summary_billed", SUMMARY_BILLED));
  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("550");
  expect(sentence).toContain("500");
  expect(sentence).toContain("50");
  expect(sentence).toContain("0.010500");
  expect(sentence).toContain("USD");
  expect(sentence).toContain("acme-large");
  expect(sentence).toContain(SUMMARY_BILLED.endpoint_id);
  expect(fill(t(said?.key ?? "appName"), said?.values ?? {})).toContain("acme-large");
});

test("a billed summary call with no configured price says cost is unknown, never 0", () => {
  const said = eventNote(
    frame("context_summary_billed", {
      ...SUMMARY_BILLED,
      cost: null,
      cost_currency: null,
      cost_quality: "unknown",
    }),
  );
  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("unknown");
  expect(fill(t(said?.key ?? "appName"), said?.values ?? {})).toContain("未知");
  // The known-cost sentence, not this one — the two must not collapse into
  // the same words the way `context_compacted`'s three sources must not.
  expect(said?.key).not.toBe(eventNote(frame("context_summary_billed", SUMMARY_BILLED))?.key);
});

test("a billed summary call with no reported usage still names the call itself", () => {
  // The call still moved `consumed_model_calls` even when the provider
  // reported nothing to bill — see `_bill_summary_call`'s own reasoning.
  const said = eventNote(
    frame("context_summary_billed", {
      ...SUMMARY_BILLED,
      input_tokens: null,
      output_tokens: null,
      tokens: 0,
      cost: null,
      cost_currency: null,
      cost_quality: "unknown",
    }),
  );
  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("—");
  expect(sentence).not.toContain("{");
});

test("a summary-billed payload this console does not fully understand gets no sentence", () => {
  expect(eventNote(frame("context_summary_billed", { model: "x" }))).toBeNull();
});

test("each trimmable segment has its own words", () => {
  for (const segment of ["old_tool_results", "skill_summaries", "memory"]) {
    expect(eventNote(frame("context_trimmed", { ...TRIM, segment }))).not.toBeNull();
  }
});

test("a payload this console does not fully understand gets no sentence", () => {
  // A newer server could trim a segment this build has never heard of, or say
  // it in fields this build does not read. Either way the entry still carries
  // its raw payload, and a half-written sentence would be worse than none.
  expect(eventNote(frame("context_trimmed", { ...TRIM, segment: "something_newer" }))).toBeNull();
  expect(eventNote(frame("context_trimmed", { segment: "memory" }))).toBeNull();
  expect(eventNote(frame("context_compacted", { first_sequence: 1 }))).toBeNull();
});

test("an ordinary event is left to speak for itself", () => {
  expect(eventNote(frame("run_started", {}))).toBeNull();
  expect(eventNote(frame("model_call_completed", { round_index: 1 }))).toBeNull();
});

const LOADED = { skill: "rollout", path: "SKILL.md", skill_version_id: "v1", bytes: 812 };

test("a loaded skill says which document entered the conversation", () => {
  const said = eventNote(frame("skill_loaded", LOADED));
  const sentence = fill(t(said?.key ?? "appName"), said?.values ?? {});
  expect(sentence).toContain("rollout");
  expect(sentence).toContain("SKILL.md");
  expect(sentence).toContain("812");
  // The boundary the prompt itself carries, repeated for the person reading
  // the timeline: this text is a workspace's, not the platform's.
  expect(sentence).toContain("参考资料");
  expect(sentence).not.toContain("{");
});

test("a skill load missing its fields gets no sentence", () => {
  expect(eventNote(frame("skill_loaded", { skill: "rollout" }))).toBeNull();
  expect(eventNote(frame("skill_loaded", { ...LOADED, path: 7 }))).toBeNull();
});

test("a proposal says that nothing changed yet", () => {
  const said = eventNote(frame("skill_proposed", { proposal_id: "p1", skill: "rollout" }));
  const sentence = fill(enUS[said?.key ?? "appName"], said?.values ?? {});
  expect(sentence).toContain("approves");
  expect(sentence).not.toContain("{");
});

test("a run waiting on its children gets its own note, not the generic one", () => {
  // Its own because the two call for opposite things from the reader: the
  // generic note sends them to find out what is being waited on, and here the
  // answer is on the same page, one card down, with links.
  expect(statusNote(situation("waiting_external", { wait: "child_runs" }))).toBe(
    "waitingChildRunsNote",
  );
});

test("the note for a child wait says it will not wake itself", () => {
  // The sentence a person reads has to answer "am I holding this up". For this
  // wait the answer is no, and the reason is that the children are running —
  // both must survive translation, which is why this asserts the text.
  for (const said of [t("waitingChildRunsNote"), enUS.waitingChildRunsNote]) {
    expect(said).toMatch(/不会醒|does not wake itself/);
  }
});

test("a delegation says how many and whether the rest get cancelled", () => {
  const all = eventNote({
    event_type: "run_delegated",
    payload: { wait: "all", children: [{ run_id: "a" }, { run_id: "b" }] },
  });
  expect(all).toEqual({ key: "delegatedAllNote", values: { count: "2" } });

  const any = eventNote({
    event_type: "run_delegated",
    payload: { wait: "any", children: [{ run_id: "a" }, { run_id: "b" }] },
  });
  expect(any).toEqual({ key: "delegatedAnyNote", values: { count: "2" } });

  // The cost of `any` is that a child about to succeed may be killed. It is
  // stated where somebody reads the timeline rather than left to a bill.
  for (const said of [t("delegatedAnyNote"), enUS.delegatedAnyNote]) {
    expect(said).toMatch(/取消|cancelled/);
  }
});

test("a delegation the console cannot read fully gets no sentence", () => {
  // Same rule as every other note: a sentence with a hole in it reads like a
  // bug in the platform rather than a fact about the Run.
  expect(eventNote({ event_type: "run_delegated", payload: { wait: "all" } })).toBeNull();
  expect(
    eventNote({ event_type: "run_delegated", payload: { children: [] } }),
  ).toBeNull();
});

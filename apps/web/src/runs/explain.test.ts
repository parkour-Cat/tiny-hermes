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

import { expect, test } from "vitest";

import { outcomeLabel, statusNote } from "./explain";

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

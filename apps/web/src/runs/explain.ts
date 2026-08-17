import type { RunResponse } from "../api/types";
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
    return run.wait_kind === "timer" ? "waitingTimerNote" : "waitingExternalNote";
  }
  if (run.status === "paused" && run.pause_reason === "limit") {
    return "pausedLimitNote";
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

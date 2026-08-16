import type { MessageKey } from "../i18n/zh-CN";

/**
 * What the console offers for each action the platform reports.
 *
 * Keyed by the action names the state machine produces. An action this table
 * does not know is not rendered: its request shape would be a guess.
 */
export type RunOffer = {
  label: MessageKey;
  question: MessageKey | null;
  done: MessageKey | null;
};

export const RUN_ACTIONS: Partial<Record<string, RunOffer>> = {
  pause: { label: "pauseRun", question: null, done: "pauseRequested" },
  resume: { label: "resumeRun", question: null, done: "resumeRequested" },
  cancel: { label: "cancelRun", question: "cancelRunWarning", done: "cancelRequested" },
  retry: { label: "retryRun", question: "retryRunWarning", done: null },
};

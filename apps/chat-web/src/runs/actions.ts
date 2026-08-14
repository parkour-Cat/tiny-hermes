import type { MessageKey } from "../i18n/zh-CN";

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

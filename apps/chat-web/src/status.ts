import type { MessageKey } from "./i18n/zh-CN";

const STATUS_KEYS: Record<string, MessageKey> = {
  queued: "statusQueued",
  running: "statusRunning",
  waiting_approval: "statusWaitingApproval",
  waiting_external: "statusWaitingExternal",
  cancelling: "statusCancelling",
  paused: "statusPaused",
  completed: "statusCompleted",
  failed: "statusFailed",
  cancelled: "statusCancelled",
  interrupted: "statusInterrupted",
  waiting: "statusWaiting",
  session_blocked: "statusSessionBlocked",
};

export function statusLabel(code: string, t: (key: MessageKey) => string): string {
  const key = STATUS_KEYS[code];
  return key === undefined ? code : t(key);
}

// A run waiting on an approval or an outside answer has not finished: the
// person can still stop it or add to it (cancel_end_user_run and
// steer_end_user_run accept any state short of terminal).
export function isLiveStatus(code: string | undefined): boolean {
  return code === "queued" || code === "running" || code === "waiting"
    || code === "waiting_approval" || code === "waiting_external";
}

import type { MessageKey } from "./i18n/zh-CN";

const STATUS_KEYS: Record<string, MessageKey> = {
  queued: "statusQueued",
  running: "statusRunning",
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

export function isLiveStatus(code: string | undefined): boolean {
  return code === "queued" || code === "running" || code === "waiting";
}

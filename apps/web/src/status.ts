import type { MessageKey } from "./i18n/zh-CN";

/**
 * Operator-facing labels for state-machine and account codes.
 *
 * Unknown codes stay as themselves: inventing a translation would put a second
 * vocabulary between the operator and the events they are reading. Protocol
 * names (`continue_once`, `file.list`, `run_completed`) are not in this table.
 */
const STATUS_KEYS: Record<string, MessageKey> = {
  queued: "statusQueued",
  running: "statusRunning",
  paused: "statusPaused",
  completed: "statusCompleted",
  failed: "statusFailed",
  cancelled: "statusCancelled",
  interrupted: "statusInterrupted",
  waiting: "statusWaiting",
  head: "statusHead",
  terminal: "statusTerminal",
  session_blocked: "statusSessionBlocked",
  active: "statusActive",
  disabled: "statusDisabled",
  user: "roleUser",
  assistant: "roleAssistant",
  tool: "roleTool",
  system: "roleSystem",
  developer: "memberRoleDeveloper",
  viewer: "memberRoleViewer",
  workspace: "secretScopeWorkspace",
  platform: "secretScopePlatform",
};

export function statusLabel(code: string, t: (key: MessageKey) => string): string {
  const key = STATUS_KEYS[code];
  return key === undefined ? code : t(key);
}

import type { CanonicalMessage } from "../api/types";
import { textOf } from "../runs/transcript";

/**
 * A conversation label a person can scan.
 *
 * The platform has no session title field. The first user line is the
 * ordinary chat convention; an empty thread stays "新对话", not a UUID.
 */
export function isBlankSession(
  session: { head_run_id: string | null; next_run_sequence: number },
  messages?: CanonicalMessage[],
): boolean {
  if (messages !== undefined) {
    return sessionTitle(messages, "") === "";
  }
  return session.head_run_id === null && session.next_run_sequence === 1;
}

export function sessionTitle(messages: CanonicalMessage[], emptyLabel: string): string {
  for (const message of messages) {
    if (message.role !== "user") {
      continue;
    }
    const line = textOf(message).trim().split("\n")[0] ?? "";
    if (line === "") {
      continue;
    }
    return line.length > 36 ? `${line.slice(0, 35)}…` : line;
  }
  return emptyLabel;
}

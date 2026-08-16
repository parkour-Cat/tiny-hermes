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
    // Long enough to be a useful tooltip; the rail truncates what it shows
    // with an ellipsis rather than cutting the string here.
    return line.length > 80 ? `${line.slice(0, 79)}…` : line;
  }
  return emptyLabel;
}

import type { CanonicalMessage, CanonicalMessagePart } from "../api/types";

export type ToolRound = {
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  output: string;
  status: "pending" | "succeeded" | "failed" | "returned";
};

export function textOf(message: CanonicalMessage): string {
  return message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("");
}

/**
 * Calls paired with their results across messages: the call is in an
 * assistant message and its result in the `tool` message after it, so a
 * reply's rounds only come together when its messages are read as one.
 */
export function toolsOf(messages: CanonicalMessage[]): ToolRound[] {
  const rounds: ToolRound[] = [];
  const byCall = new Map<string, ToolRound>();
  for (const message of messages) {
    for (const part of message.parts) {
      const round = roundFrom(part);
      if (round === null) {
        continue;
      }
      const existing = byCall.get(round.callId);
      if (existing === undefined) {
        byCall.set(round.callId, round);
        rounds.push(round);
        continue;
      }
      if (round.name !== "") {
        existing.name = round.name;
        existing.arguments = round.arguments;
      }
      if (round.status !== "pending") {
        existing.output = round.output;
        existing.status = round.status;
      }
    }
  }
  return rounds;
}

function roundFrom(part: CanonicalMessagePart): ToolRound | null {
  if (part.type === "tool_call") {
    return {
      callId: part.call_id ?? "",
      name: part.name ?? "",
      arguments: part.arguments ?? {},
      output: "",
      status: "pending",
    };
  }
  if (part.type === "tool_result") {
    return {
      callId: part.call_id ?? "",
      name: "",
      arguments: {},
      output: part.output ?? "",
      status: resultStatus(part),
    };
  }
  return null;
}

/** The console's reading (`apps/web/src/runs/transcript.ts`): only an
 *  explicit flag or exit code earns a success or failure mark. */
function resultStatus(part: CanonicalMessagePart): ToolRound["status"] {
  if (part.failed === true || (part.exit_code !== undefined && part.exit_code !== 0)) return "failed";
  if (part.failed === false || part.exit_code === 0) return "succeeded";
  return "returned";
}

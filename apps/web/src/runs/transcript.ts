import type { ArtifactResponse, CanonicalMessage, CanonicalMessagePart } from "../api/types";

const ARTIFACT_ID = /artifact_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/gi;

export type ToolRound = {
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  output: string;
  status: "pending" | "succeeded" | "failed" | "returned";
  artifactIds: string[];
};

export function artifactIdsIn(text: string): string[] {
  return [...text.matchAll(ARTIFACT_ID)].flatMap((match) =>
    match[1] === undefined ? [] : [match[1]],
  );
}

export function textOf(message: CanonicalMessage): string {
  return message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("");
}

/** How much of a tool's output a transcript line carries. The tools section
 * below the transcript holds the whole thing; this line exists to keep the
 * causal chain readable, and a 40 KB shell dump in the middle of it does the
 * opposite. */
const OUTPUT_PREVIEW = 160;

/**
 * One turn, as a line a person can read.
 *
 * Separate from `textOf` rather than an extension of it. `textOf` means "the
 * words somebody said", and `chat-web` uses `textOf(message) !== ""` to
 * decide whether a turn is worth showing at all — widening it to include tool
 * calls would put internal state on an end-user surface that §19.1 keeps it
 * off.
 *
 * A turn with nothing readable still returns a marker rather than an empty
 * string. A blank row reads as a bug in the page, which is how the original
 * problem was reported.
 */
export function transcriptLineOf(message: CanonicalMessage): string {
  const said = message.parts.flatMap((part) => {
    if (part.type === "text") {
      return part.text === undefined || part.text === "" ? [] : [part.text];
    }
    if (part.type === "tool_call") {
      return [`→ ${part.name ?? "?"}`];
    }
    if (part.type === "tool_result") {
      // Only explicit result status warrants a success/failure marker.
      // Older messages without status still read as a returned result.
      const status = resultStatus(part);
      const mark = status === "failed" ? "✗" : status === "succeeded" ? "✓" : "←";
      return [`${mark} ${preview(part.output ?? "") || "—"}`];
    }
    return [];
  });
  return said.length === 0 ? "—" : said.join("\n");
}

function preview(output: string): string {
  const flat = output.trim();
  return flat.length <= OUTPUT_PREVIEW ? flat : `${flat.slice(0, OUTPUT_PREVIEW)}…`;
}

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
        existing.artifactIds = round.artifactIds;
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
      artifactIds: [],
    };
  }
  if (part.type === "tool_result") {
    const output = part.output ?? "";
    return {
      callId: part.call_id ?? "",
      name: "",
      arguments: {},
      output,
      status: resultStatus(part),
      artifactIds: artifactIdsIn(output),
    };
  }
  return null;
}

function resultStatus(part: CanonicalMessagePart): ToolRound["status"] {
  if (part.failed === true || (part.exit_code !== undefined && part.exit_code !== 0)) return "failed";
  if (part.failed === false || part.exit_code === 0) return "succeeded";
  return "returned";
}

export function mergeArtifacts(
  listed: ArtifactResponse[],
  fromText: string[],
): { id: string; filename: string }[] {
  const seen = new Set<string>();
  const rows: { id: string; filename: string }[] = [];
  for (const item of listed) {
    if (seen.has(item.id)) {
      continue;
    }
    seen.add(item.id);
    rows.push({ id: item.id, filename: item.filename });
  }
  for (const id of fromText) {
    if (seen.has(id)) {
      continue;
    }
    seen.add(id);
    rows.push({ id, filename: id });
  }
  return rows;
}

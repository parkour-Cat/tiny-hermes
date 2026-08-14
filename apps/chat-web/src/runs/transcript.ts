import type { ArtifactResponse, CanonicalMessage, CanonicalMessagePart } from "../api/types";

const ARTIFACT_ID = /artifact_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/gi;

export type ToolRound = {
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  output: string;
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
      if (round.output !== "") {
        existing.output = round.output;
        existing.artifactIds = round.artifactIds;
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
      artifactIds: artifactIdsIn(output),
    };
  }
  return null;
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

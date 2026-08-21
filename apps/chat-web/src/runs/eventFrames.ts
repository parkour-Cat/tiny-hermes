import type { RunEventFrame } from "../api/types";

/**
 * The incremental reader for the Run Event stream's wire format.
 *
 * Pure, and separate from the socket that feeds it: a frame arriving in two
 * pieces is the ordinary case over a real connection, not an edge case, and a
 * reader that parses whatever a single chunk happens to contain corrupts
 * payloads under exactly the load that matters. Everything here is a string in
 * and frames out, so that behaviour is testable without a network.
 */

/**
 * Reads every complete frame out of a buffer.
 *
 * The caller keeps `rest` and prepends the next chunk to it. Frames are
 * separated by a blank line, so anything after the last one is a frame still
 * arriving and is never guessed at.
 */
export function readFrames(buffer: string): { frames: RunEventFrame[]; rest: string } {
  const boundary = buffer.lastIndexOf("\n\n");
  if (boundary === -1) {
    return { frames: [], rest: buffer };
  }
  const frames: RunEventFrame[] = [];
  for (const block of buffer.slice(0, boundary).split("\n\n")) {
    const frame = readFrame(block);
    if (frame !== null) {
      frames.push(frame);
    }
  }
  return { frames, rest: buffer.slice(boundary + 2) };
}

/**
 * One frame, or `null` for a block that carries no data.
 *
 * A heartbeat is a comment line and a blank line, which is a well-formed block
 * with nothing in it; skipping it here means the buffer needs no special case.
 * A data line that is not the JSON the server documents is not skipped — it
 * throws, because dropping it would put a hole in a timeline that still looks
 * complete.
 */
function readFrame(block: string): RunEventFrame | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice("data:".length).trimStart())
    .join("\n");
  if (data === "") {
    return null;
  }
  const frame = JSON.parse(data) as RunEventFrame;
  if (typeof frame.sequence !== "number" || typeof frame.event_type !== "string") {
    throw new Error("A stream frame arrived without a sequence or a type.");
  }
  return frame;
}

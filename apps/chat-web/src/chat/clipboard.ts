import { stagedFile, type StagedFile } from "./attachments";

export type ClipboardPayload =
  | { ok: true; files: StagedFile[]; text: string }
  | { ok: false; reason: "denied" | "empty" };

function filenameFor(type: string, index: number): string {
  const ext = type.split("/")[1]?.split("+")[0] ?? "bin";
  return `clipboard-${index + 1}.${ext}`;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export async function readClipboardPayload(): Promise<ClipboardPayload> {
  const clipboard = navigator.clipboard;
  if (clipboard === undefined) {
    return { ok: false, reason: "denied" };
  }
  const files: StagedFile[] = [];
  let text = "";
  try {
    if (clipboard.read !== undefined) {
      const items = await clipboard.read();
      for (const item of items) {
        for (const type of item.types) {
          const blob = await item.getType(type);
          if (type === "text/plain") {
            if (text === "") {
              text = await blob.text();
            }
            continue;
          }
          if (type === "text/html") {
            continue;
          }
          const name = filenameFor(type, files.length);
          files.push(stagedFile(new File([blob], name, { type })));
        }
      }
    }
    if (text === "" && clipboard.readText !== undefined) {
      text = await clipboard.readText();
    }
  } catch {
    return { ok: false, reason: "denied" };
  }
  if (files.length === 0 && text.trim() === "") {
    return { ok: false, reason: "empty" };
  }
  return { ok: true, files, text };
}

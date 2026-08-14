const TEXT_TYPES = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/tab-separated-values",
  "application/json",
  "application/xml",
  "text/xml",
]);

const TEXT_NAMES = /\.(txt|md|markdown|csv|tsv|json|xml|log|yml|yaml|toml|ini)$/i;
const MAX_CHARS = 12_000;

export type StagedFile = {
  name: string;
  size: number;
  type: string;
  file: File;
};

export function canInlineAttachment(file: Pick<File, "name" | "type">): boolean {
  return TEXT_TYPES.has(file.type) || TEXT_NAMES.test(file.name);
}

export function stagedFile(file: File): StagedFile {
  return { name: file.name, size: file.size, type: file.type, file };
}

export function stagedFromList(list: FileList | File[] | null | undefined): StagedFile[] {
  return [...(list ?? [])].map(stagedFile);
}

export function mergeStaged(current: StagedFile[], incoming: StagedFile[], limit = 8): StagedFile[] {
  return [...current, ...incoming].slice(0, limit);
}

export async function composeWithAttachments(
  text: string,
  files: StagedFile[],
): Promise<{ text: string; skipped: string[] }> {
  const skipped: string[] = [];
  const chunks: string[] = [];
  if (text.trim() !== "") {
    chunks.push(text.trim());
  }
  for (const item of files) {
    if (!canInlineAttachment(item)) {
      skipped.push(item.name);
      continue;
    }
    const raw = await item.file.text();
    const body = raw.length > MAX_CHARS ? `${raw.slice(0, MAX_CHARS)}\n…` : raw;
    chunks.push(`附件 ${item.name}:\n${body}`);
  }
  return { text: chunks.join("\n\n"), skipped };
}

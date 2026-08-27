import type { CanonicalMessage } from "../api/types";
import { textOf, toolsOf } from "../runs/transcript";

export function exportFilename(agentAlias: string, sessionId: string | null): string {
  const short = sessionId === null ? "chat" : sessionId.slice(0, 8);
  const safe =
    agentAlias.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") ||
    "chat";
  return `${safe}-${short}.md`;
}

export function transcriptMarkdown(
  title: string,
  turns: CanonicalMessage[],
  labels: { user: string; agent: string; withdrawn: string },
): string {
  const blocks = [`# ${title}`, ""];
  for (const message of turns) {
    const heading =
      message.role === "user" ? labels.user : message.role === "assistant" ? labels.agent : message.role;
    const text = textOf(message);
    const tools = toolsOf([message]);
    if (text === "" && tools.length === 0) {
      continue;
    }
    // 导出的那份是用户留在手上的。界面上标了、导出里没标，等于把一个
    // 比界面更持久的误会交给他。
    const mark = message.withdrawn_at == null ? "" : ` (${labels.withdrawn})`;
    blocks.push(`## ${heading}${mark}`, "");
    if (text !== "") {
      blocks.push(text, "");
    }
    for (const tool of tools) {
      blocks.push(`### ${tool.name}`, "");
      if (Object.keys(tool.arguments).length > 0) {
        blocks.push("```json", JSON.stringify(tool.arguments, null, 2), "```", "");
      }
      if (tool.output !== "") {
        blocks.push(tool.output, "");
      }
    }
  }
  return `${blocks.join("\n").trim()}\n`;
}

export function downloadMarkdown(filename: string, markdown: string): void {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

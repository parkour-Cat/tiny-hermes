import type { MessageKey } from "../i18n/zh-CN";
import { fill } from "./explain";
import type { ToolRound } from "./transcript";

const DETAIL = 120;

/** What a platform tool does, said the way the person would say it. */
const PLATFORM: Record<string, { title: MessageKey; argument: string }> = {
  "shell.exec": { title: "stepShell", argument: "command" },
  "file.read": { title: "stepFileRead", argument: "path" },
  "file.write": { title: "stepFileWrite", argument: "path" },
  "file.list": { title: "stepFileList", argument: "path" },
  "file.send": { title: "stepFileSend", argument: "path" },
  "session.search": { title: "stepSessionSearch", argument: "query" },
  "site.publish": { title: "stepSitePublish", argument: "path" },
  "skill.load": { title: "stepSkillLoad", argument: "name" },
  "agent.delegate": { title: "stepDelegate", argument: "input" },
};

/**
 * One tool call as a line in the run's 过程: a title a person can read, and
 * the one argument that says what this call was about — the command, the
 * file, the search words. The tool's own name stays beside it on the page;
 * this is a reading aid, not a replacement for what was called.
 */
export function stepOf(round: ToolRound, t: (key: MessageKey) => string): { title: string; detail: string } {
  const known = PLATFORM[round.name];
  if (known !== undefined) {
    return { title: t(known.title), detail: cut(firstLine(text(round.arguments[known.argument]))) };
  }
  const http = /^http\.([^.]+)\.(.+)$/.exec(round.name);
  if (http !== null) {
    return {
      title: fill(t("stepHttp"), { tool: http[1]!, operation: http[2]! }),
      detail: cut(queryOf(round.arguments) ?? compact(round.arguments)),
    };
  }
  return { title: round.name, detail: Object.keys(round.arguments).length === 0 ? "" : cut(compact(round.arguments)) };
}

/** The search words, wherever an HTTP call carried them. */
function queryOf(args: Record<string, unknown>): string | null {
  for (const holder of [args.body, args, args.query]) {
    if (holder !== null && typeof holder === "object") {
      const query = (holder as Record<string, unknown>).query ?? (holder as Record<string, unknown>).q;
      if (typeof query === "string") return query;
    }
  }
  return null;
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  return value === undefined ? "" : compact(value);
}

function compact(value: unknown): string {
  return JSON.stringify(value) ?? "";
}

function firstLine(value: string): string {
  return value.split("\n", 1)[0]!.trim();
}

function cut(value: string): string {
  return value.length <= DETAIL ? value : `${value.slice(0, DETAIL)}…`;
}

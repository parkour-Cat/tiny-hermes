import { useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { copyText } from "./clipboard";
import type { SavedFile } from "./useSavedFiles";
import type { CanonicalMessage } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { stepOf } from "../runs/steps";
import { textOf, toolsOf, type ToolRound } from "../runs/transcript";
import { HermesMark } from "../ui/HermesMark";

// A reply is model output, and a prompt injection can shape it. Markdown
// renders no raw HTML and drops `javascript:` URLs on its own; an image is
// turned into a link because it would load — and could carry the
// conversation in its URL — the moment it rendered, without anyone choosing to.
const REPLY_COMPONENTS: Components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
  ),
  img: ({ src, alt }) => {
    const href = typeof src === "string" ? src : undefined;
    return <a href={href} target="_blank" rel="noopener noreferrer">{alt || href}</a>;
  },
};

function Prose({ text }: { text: string }) {
  return (
    <div className="prose">
      <Markdown remarkPlugins={[remarkGfm]} components={REPLY_COMPONENTS}>{text}</Markdown>
    </div>
  );
}

type Entry =
  | { kind: "user"; index: number; message: CanonicalMessage }
  | { kind: "reply"; index: number; messages: CanonicalMessage[] };

/**
 * Everything between two things the person said is one reply.
 *
 * A reply is several stored messages — each call in an assistant message, its
 * result in a `tool` message after it, the answer in a last assistant one.
 * Drawn one message at a time, a call never meets its result: its card
 * waits for output forever and the result shows up as a second, nameless one.
 */
function entriesOf(turns: CanonicalMessage[]): Entry[] {
  const entries: Entry[] = [];
  turns.forEach((message, index) => {
    if (message.role === "user") {
      entries.push({ kind: "user", index, message });
      return;
    }
    const last = entries[entries.length - 1];
    if (last?.kind === "reply") last.messages.push(message);
    else entries.push({ kind: "reply", index, messages: [message] });
  });
  return entries;
}

function textsOf(messages: CanonicalMessage[]): string[] {
  return messages.flatMap((message) => {
    const text = message.role === "assistant" ? textOf(message) : "";
    return text === "" ? [] : [text];
  });
}

function fill(said: string, n: number): string {
  return said.replace("{n}", String(n));
}

function sizeOf(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The steps of one reply, folded to a line once it has finished. */
function Work({ tools, working, t }: { tools: ToolRound[]; working: boolean; t: (key: MessageKey) => string }) {
  const failed = tools.filter((tool) => tool.status === "failed").length;
  return (
    <details className="work" open={working || undefined}>
      <summary>
        {working ? fill(t("workLive"), tools.length) : fill(t("workDone"), tools.length)}
        {failed === 0 ? null : <span className="work-failed"> · {fill(t("workFailed"), failed)}</span>}
      </summary>
      <ol className="work-steps">
        {tools.map((tool) => {
          const step = stepOf(tool, t);
          return (
            <li key={tool.callId || tool.name} className={`work-step work-step-${tool.status}`}>
              <span className="work-step-title">{step.title}</span>
              {step.detail === "" ? null : <code className="work-step-detail">{step.detail}</code>}
              {tool.output === "" ? null : (
                <details className="work-step-output">
                  <summary>{t("stepOutput")}</summary>
                  <pre>{tool.output}</pre>
                </details>
              )}
            </li>
          );
        })}
      </ol>
    </details>
  );
}

/**
 * A file the reply sent back, read from the `file.send` call's path. Outside
 * Feishu nothing is delivered — the file is in the session's files — so the
 * card downloads from there, and says so when it is no longer among them
 * rather than offering a button that can only fail.
 */
function FileCard({
  path,
  saved,
  saving,
  onSave,
  t,
}: {
  path: string;
  saved: SavedFile | undefined;
  saving: boolean;
  onSave: () => void;
  t: (key: MessageKey) => string;
}) {
  const name = path.split("/").pop() ?? path;
  const extension = name.includes(".") ? (name.split(".").pop() ?? "").slice(0, 4).toUpperCase() : "";
  return (
    <div className="file-card" role="group" aria-label={name}>
      <span className="file-card-kind" aria-hidden="true">{extension || "FILE"}</span>
      <span className="file-card-text">
        <span className="file-card-name">{name}</span>
        <span className="file-card-meta">{saved === undefined ? t("fileGone") : sizeOf(saved.size_bytes)}</span>
      </span>
      {saved === undefined ? null : (
        <button type="button" disabled={saving} onClick={onSave}>
          {saving ? t("savedFileDownloading") : t("savedFileDownload")}
        </button>
      )}
    </div>
  );
}

export function Transcript({
  turns,
  optimistic,
  live,
  savedFiles,
  savingFile,
  canRetry,
  onSaveFile,
  onRetry,
}: {
  turns: CanonicalMessage[];
  optimistic: string | null;
  live: boolean;
  savedFiles: SavedFile[];
  savingFile: string | null;
  canRetry: boolean;
  onSaveFile: (path: string) => void;
  onRetry: () => void;
}) {
  const t = useT();
  const end = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const entries = entriesOf(turns);
  const visible = entries.filter(
    (entry) => entry.kind === "user" || textsOf(entry.messages).length > 0 || toolsOf(entry.messages).length > 0,
  );
  const lastReply = [...visible].reverse().find((entry) => entry.kind === "reply");
  const lastEntry = visible[visible.length - 1];

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [turns, optimistic, live]);

  async function copy(key: string, text: string): Promise<void> {
    if (await copyText(text)) {
      setCopied(key);
    }
  }

  if (turns.length === 0 && optimistic === null) {
    return (
      <div className="thread-empty">
        <HermesMark variant="empty" size={96} />
        <p className="empty-lead">{t("greeting")}</p>
        <p>{t("emptyIntro")}</p>
        {live ? (
          <p className="live-dot" role="status">
            {t("replying")}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="thread">
      {visible.map((entry) => {
        if (entry.kind === "user") {
          const text = textOf(entry.message);
          const key = `user-${entry.index}`;
          return (
            <div className="bubble-wrap" key={key}>
              <article className="bubble-user">{text}</article>
              {entry.message.withdrawn_at == null ? null : (
                /* 这一轮留在转写里，因为它确实被说过；这个标记是它和模型
                   仍在读的一轮之间唯一的区别。 */
                <p className="fact-note">{t("withdrawnTurn")}</p>
              )}
              <div className="msg-actions">
                <button type="button" onClick={() => void copy(key, text)}>
                  {copied === key ? t("copied") : t("copyMessage")}
                </button>
              </div>
            </div>
          );
        }
        const texts = textsOf(entry.messages);
        const text = texts.join("\n\n");
        const tools = toolsOf(entry.messages);
        const sent = tools.filter((tool) => tool.name === "file.send" && tool.status === "succeeded");
        const key = `agent-${entry.index}`;
        const showRetry = canRetry && !live && entry === lastReply;
        // Only the reply the run is still adding to is open; an earlier one
        // that ended mid-step stays folded with what it got through.
        const working = live && entry === lastEntry && tools.some((tool) => tool.status === "pending");
        return (
          <article className="turn-agent" key={key}>
            <HermesMark size={22} />
            <div className="turn-body">
              {entry.messages.every((message) => message.withdrawn_at == null) ? null : (
                /* 与用户那一侧同一个理由：留着，但要看得出模型已经不读它了。 */
                <p className="fact-note">{t("withdrawnTurn")}</p>
              )}
              {tools.length === 0 ? null : <Work tools={tools} working={working} t={t} />}
              {texts.map((said, index) => <Prose key={`${key}-${index}`} text={said} />)}
              {sent.map((tool) => {
                const path = typeof tool.arguments.path === "string" ? tool.arguments.path : "";
                return path === "" ? null : (
                  <FileCard
                    key={tool.callId}
                    path={path}
                    saved={savedFiles.find((file) => file.path === path)}
                    saving={savingFile === path}
                    onSave={() => onSaveFile(path)}
                    t={t}
                  />
                );
              })}
              <div className="msg-actions">
                {text === "" ? null : (
                  <button type="button" onClick={() => void copy(key, text)}>
                    {copied === key ? t("copied") : t("copyMessage")}
                  </button>
                )}
                {showRetry && confirmRetry ? (
                  <>
                    <span className="msg-hint">{t("retryRunWarning")}</span>
                    <button
                      type="button"
                      onClick={() => {
                        setConfirmRetry(false);
                        onRetry();
                      }}
                    >
                      {t("retryRunNow")}
                    </button>
                    <button type="button" onClick={() => setConfirmRetry(false)}>
                      {t("cancel")}
                    </button>
                  </>
                ) : null}
                {showRetry && !confirmRetry ? (
                  <button type="button" onClick={() => setConfirmRetry(true)}>
                    {t("retryRun")}
                  </button>
                ) : null}
              </div>
            </div>
          </article>
        );
      })}
      {optimistic === null ? null : <article className="bubble-user">{optimistic}</article>}
      {live ? (
        <p className="live-dot" role="status">
          {t("replying")}
        </p>
      ) : null}
      <div ref={end} />
    </div>
  );
}

import { useEffect, useRef, useState } from "react";

import { copyText } from "./clipboard";
import type { ArtifactResponse, CanonicalMessage } from "../api/types";
import { useT } from "../i18n/locale";
import { mergeArtifacts, textOf, toolsOf } from "../runs/transcript";
import { HermesMark } from "../ui/HermesMark";

function Prose({ text }: { text: string }) {
  return (
    <div className="prose">
      {text.split("\n").map((line, index) => (
        <p key={`${index}-${line}`}>{line === "" ? "\u00a0" : line}</p>
      ))}
    </div>
  );
}

function lastVisibleAssistant(turns: CanonicalMessage[]): number {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const message = turns[index];
    if (message === undefined || message.role !== "assistant") {
      continue;
    }
    if (textOf(message) !== "" || toolsOf([message]).length > 0) {
      return index;
    }
  }
  return -1;
}

export function Transcript({
  turns,
  optimistic,
  live,
  artifacts,
  canRetry,
  onDownload,
  onRetry,
}: {
  turns: CanonicalMessage[];
  optimistic: string | null;
  live: boolean;
  artifacts: ArtifactResponse[];
  canRetry: boolean;
  onDownload: (id: string, filename: string) => void;
  onRetry: () => void;
}) {
  const t = useT();
  const end = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const retryAt = lastVisibleAssistant(turns);
  const files = mergeArtifacts(
    artifacts,
    turns.flatMap((message) =>
      message.parts.flatMap((part) => (part.output === undefined ? [] : [part.output])),
    ),
  );

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [turns, optimistic, live, files.length]);

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
      {turns.map((message, index) => {
        if (message.role === "user") {
          const text = textOf(message);
          const key = `user-${index}`;
          return (
            <div className="bubble-wrap" key={key}>
              <article className="bubble-user">{text}</article>
              {message.withdrawn_at == null ? null : (
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
        const text = textOf(message);
        const tools = toolsOf([message]);
        if (text === "" && tools.length === 0) {
          return null;
        }
        const key = `agent-${index}`;
        const showRetry = canRetry && !live && index === retryAt;
        return (
          <article className="turn-agent" key={key}>
            <HermesMark size={22} />
            <div className="turn-body">
              {message.withdrawn_at == null ? null : (
                /* 与用户那一侧同一个理由：留着，但要看得出模型已经不读它了。 */
                <p className="fact-note">{t("withdrawnTurn")}</p>
              )}
              {text === "" ? null : <Prose text={text} />}
              {tools.map((tool) => (
                <details className="tool-card" key={tool.callId || tool.name} open={tool.output === ""}>
                  <summary>{tool.name}</summary>
                  {Object.keys(tool.arguments).length === 0 ? null : (
                    <pre>{JSON.stringify(tool.arguments, null, 2)}</pre>
                  )}
                  {tool.output === "" ? null : <pre>{tool.output}</pre>}
                </details>
              ))}
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
      {files.length === 0 ? null : (
        <div className="thread-files">
          {files.map((file) => (
            <button type="button" key={file.id} onClick={() => onDownload(file.id, file.filename)}>
              {file.filename}
            </button>
          ))}
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

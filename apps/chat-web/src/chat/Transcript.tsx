import { useEffect, useRef } from "react";

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

export function Transcript({
  turns,
  optimistic,
  live,
  artifacts,
  onDownload,
}: {
  turns: CanonicalMessage[];
  optimistic: string | null;
  live: boolean;
  artifacts: ArtifactResponse[];
  onDownload: (id: string, filename: string) => void;
}) {
  const t = useT();
  const end = useRef<HTMLDivElement>(null);
  const files = mergeArtifacts(
    artifacts,
    turns.flatMap((message) =>
      message.parts.flatMap((part) => (part.output === undefined ? [] : [part.output])),
    ),
  );

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [turns, optimistic, live, files.length]);

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
          return (
            <article className="bubble-user" key={`user-${index}`}>
              {textOf(message)}
            </article>
          );
        }
        const text = textOf(message);
        const tools = toolsOf([message]);
        if (text === "" && tools.length === 0) {
          return null;
        }
        return (
          <article className="turn-agent" key={`agent-${index}`}>
            <HermesMark size={22} />
            <div className="turn-body">
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

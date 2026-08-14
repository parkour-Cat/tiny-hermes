import { useRef, useState } from "react";

import { composeWithAttachments, type StagedFile } from "./attachments";
import { useT } from "../i18n/locale";

function fit(area: HTMLTextAreaElement): void {
  area.style.height = "0";
  area.style.height = `${Math.min(area.scrollHeight, 168)}px`;
}

export function Composer({
  disabled,
  sending,
  live,
  onSend,
  onStop,
}: {
  disabled: boolean;
  sending: boolean;
  live: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const t = useT();
  const area = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<StagedFile[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const ready = (input.trim() !== "" || files.length > 0) && !disabled && !sending && !live;

  async function submit(): Promise<void> {
    if (!ready) {
      return;
    }
    const composed = await composeWithAttachments(input, files);
    if (composed.text.trim() === "") {
      setNote(t("attachBinary"));
      return;
    }
    if (composed.skipped.length > 0) {
      setNote(`${t("attachBinary")} ${composed.skipped.join("、")}`);
    } else {
      setNote(null);
    }
    onSend(composed.text);
    setInput("");
    setFiles([]);
    if (area.current !== null) {
      area.current.style.height = "";
    }
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      {files.length > 0 ? (
        <ul className="composer-files">
          {files.map((item, index) => (
            <li key={`${item.name}-${index}`}>
              <span>{item.name}</span>
              <button
                type="button"
                aria-label={`${t("removeFile")} ${item.name}`}
                onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <textarea
        ref={area}
        aria-label={t("composerPlaceholder")}
        placeholder={t("composerPlaceholder")}
        rows={1}
        value={input}
        disabled={disabled}
        onChange={(event) => {
          setInput(event.target.value);
          fit(event.target);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void submit();
          }
        }}
      />
      {note === null ? null : <p className="composer-note">{note}</p>}
      <div className="composer-bar">
        <div className="composer-tools">
          <input
            ref={picker}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              const chosen = [...(event.target.files ?? [])].map((file) => ({
                name: file.name,
                size: file.size,
                type: file.type,
                file,
              }));
              setFiles((current) => [...current, ...chosen].slice(0, 8));
              event.target.value = "";
            }}
          />
          <button
            type="button"
            className="composer-icon"
            aria-label={t("attachFile")}
            disabled={disabled || sending || live}
            onClick={() => picker.current?.click()}
          >
            <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden>
              <path
                d="M6.2 11.4 L10.8 6.8 A2.2 2.2 0 0 0 7.7 3.7 L3.4 8 a3 3 0 0 0 4.2 4.2 L12.4 7.4"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.35"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <p className="composer-hint">{t("composerHint")}</p>
        </div>
        {live ? (
          <button type="button" className="composer-stop" onClick={onStop}>
            {t("stopReply")}
          </button>
        ) : (
          <button type="submit" disabled={!ready}>
            {t("sendMessage")}
          </button>
        )}
      </div>
    </form>
  );
}

import { useCallback, useRef, useState } from "react";

import { composeWithAttachments, mergeStaged, stagedFromList, type StagedFile } from "./attachments";
import { readClipboardPayload } from "./clipboard";
import { canDictate, startDictation } from "./speech";
import { useDismiss } from "./useDismiss";
import { useLocale } from "../i18n/locale";

function fit(area: HTMLTextAreaElement): void {
  area.style.height = "0";
  area.style.height = `${Math.min(area.scrollHeight, 168)}px`;
}

export function Composer({
  disabled,
  sending,
  live,
  canExport,
  onSend,
  onStop,
  onExport,
}: {
  disabled: boolean;
  sending: boolean;
  live: boolean;
  canExport: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onExport: () => void;
}) {
  const { t, locale } = useLocale();
  const area = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const plus = useRef<HTMLDivElement>(null);
  const dragDepth = useRef(0);
  const listening = useRef<{ stop: () => void } | null>(null);
  const draft = useRef("");
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<StagedFile[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [dictating, setDictating] = useState(false);
  const closeMenu = useCallback(() => setMenu(false), []);
  useDismiss(menu, closeMenu, plus);
  const busy = disabled || sending || live;
  const ready = (input.trim() !== "" || files.length > 0) && !busy;
  const voice = canDictate();

  function addFiles(incoming: StagedFile[]): void {
    if (incoming.length === 0) {
      return;
    }
    setFiles((current) => mergeStaged(current, incoming));
  }

  function appendText(chunk: string): void {
    const next = draft.current === "" ? chunk : `${draft.current} ${chunk}`;
    draft.current = next;
    setInput(next);
    if (area.current !== null) {
      fit(area.current);
    }
  }

  async function submit(): Promise<void> {
    if (!ready) {
      return;
    }
    stopVoice();
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
    draft.current = "";
    setInput("");
    setFiles([]);
    if (area.current !== null) {
      area.current.style.height = "";
    }
  }

  function stopVoice(): void {
    listening.current?.stop();
    listening.current = null;
    setDictating(false);
  }

  function toggleVoice(): void {
    if (dictating) {
      stopVoice();
      return;
    }
    const handle = startDictation(
      locale,
      (transcript, isFinal) => {
        if (isFinal) {
          appendText(transcript.trim());
        }
      },
      () => {
        listening.current = null;
        setDictating(false);
      },
    );
    if (handle === null) {
      return;
    }
    listening.current = handle;
    setDictating(true);
  }

  async function pasteClipboard(): Promise<void> {
    const payload = await readClipboardPayload();
    if (!payload.ok) {
      setNote(payload.reason === "empty" ? t("clipboardEmpty") : t("clipboardDenied"));
      return;
    }
    addFiles(payload.files);
    if (payload.text.trim() !== "") {
      appendText(payload.text.trim());
    }
    setNote(null);
  }

  return (
    <form
      className={`composer${dragging ? " is-drop" : ""}`}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      onDragEnter={(event) => {
        event.preventDefault();
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
      }}
      onDragLeave={(event) => {
        event.preventDefault();
        dragDepth.current -= 1;
        if (dragDepth.current <= 0) {
          dragDepth.current = 0;
          setDragging(false);
        }
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        addFiles(stagedFromList(event.dataTransfer.files));
      }}
    >
      {dragging ? <p className="composer-drop">{t("dropFiles")}</p> : null}
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
          draft.current = event.target.value;
          setInput(event.target.value);
          fit(event.target);
        }}
        onPaste={(event) => {
          const incoming = stagedFromList(event.clipboardData?.files);
          if (incoming.length === 0) {
            return;
          }
          event.preventDefault();
          addFiles(incoming);
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
              addFiles(stagedFromList(event.target.files));
              event.target.value = "";
            }}
          />
          <div className="composer-plus" ref={plus}>
            <button
              type="button"
              className="composer-icon"
              aria-label={t("composerMore")}
              aria-haspopup="menu"
              aria-expanded={menu}
              disabled={disabled}
              onClick={() => setMenu((open) => !open)}
            >
              <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden>
                <path
                  d="M8 3.2 v9.6 M3.2 8 h9.6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
            {menu ? (
              <div className="composer-menu" role="menu" aria-label={t("composerMore")}>
                <button
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onClick={() => {
                    setMenu(false);
                    picker.current?.click();
                  }}
                >
                  {t("attachFile")}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onClick={() => {
                    setMenu(false);
                    void pasteClipboard();
                  }}
                >
                  {t("pasteClipboard")}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!canExport}
                  onClick={() => {
                    setMenu(false);
                    onExport();
                  }}
                >
                  {t("exportChat")}
                </button>
              </div>
            ) : null}
          </div>
          {voice ? (
            <button
              type="button"
              className={`composer-icon${dictating ? " is-on" : ""}`}
              aria-label={dictating ? t("dictating") : t("dictate")}
              aria-pressed={dictating}
              disabled={busy}
              onClick={toggleVoice}
            >
              <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden>
                <rect
                  x="6"
                  y="2.4"
                  width="4"
                  height="7.2"
                  rx="2"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.35"
                />
                <path
                  d="M3.6 7.6 a4.4 4.4 0 0 0 8.8 0 M8 12 v1.6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.35"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          ) : null}
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

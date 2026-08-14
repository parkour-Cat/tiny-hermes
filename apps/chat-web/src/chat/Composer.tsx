import { useRef, useState } from "react";

import { useT } from "../i18n/locale";

function fit(area: HTMLTextAreaElement): void {
  area.style.height = "0";
  area.style.height = `${Math.min(area.scrollHeight, 168)}px`;
}

export function Composer({
  disabled,
  sending,
  onSend,
}: {
  disabled: boolean;
  sending: boolean;
  onSend: (text: string) => void;
}) {
  const t = useT();
  const area = useRef<HTMLTextAreaElement>(null);
  const [input, setInput] = useState("");
  const ready = input.trim() !== "" && !disabled && !sending;

  function submit(): void {
    const text = input.trim();
    if (text === "" || disabled || sending) {
      return;
    }
    onSend(text);
    setInput("");
    if (area.current !== null) {
      area.current.style.height = "";
    }
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
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
            submit();
          }
        }}
      />
      <div className="composer-bar">
        <p className="composer-hint">{t("composerHint")}</p>
        <button type="submit" disabled={!ready}>
          {t("sendMessage")}
        </button>
      </div>
    </form>
  );
}

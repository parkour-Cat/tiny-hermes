import { useState } from "react";

import { useT } from "../i18n/locale";

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
  const [input, setInput] = useState("");
  const ready = input.trim() !== "" && !disabled && !sending;

  function submit(): void {
    const text = input.trim();
    if (text === "" || disabled || sending) {
      return;
    }
    onSend(text);
    setInput("");
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
        aria-label={t("composerPlaceholder")}
        placeholder={t("composerPlaceholder")}
        rows={1}
        value={input}
        disabled={disabled}
        onChange={(event) => setInput(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <button type="submit" disabled={!ready}>
        {t("sendMessage")}
      </button>
      <p className="composer-hint">{t("composerHint")}</p>
    </form>
  );
}

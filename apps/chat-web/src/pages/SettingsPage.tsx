import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { ErasureResponse, SubjectExportResponse } from "../api/types";
import { useLocale, useT } from "../i18n/locale";
import { useChatTheme } from "../theme/ChatTheme";

function downloadJson(filename: string, value: unknown): void {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * Appearance, language, and design §4.6's "本人" row — export and erase,
 * the two self-service actions an end user has over their own data and
 * nothing else offers a door to. No account section: §4.5.1 means there is
 * no name or email this app was ever given to show.
 */
export function SettingsPage() {
  const t = useT();
  const { locale, setLocale } = useLocale();
  const theme = useChatTheme();
  const navigate = useNavigate();
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [erasing, setErasing] = useState(false);
  const [eraseError, setEraseError] = useState<string | null>(null);
  const [erased, setErased] = useState<ErasureResponse | null>(null);
  const [confirmingErase, setConfirmingErase] = useState(false);

  async function exportData(): Promise<void> {
    setExporting(true);
    setExportError(null);
    try {
      const exported = await api<SubjectExportResponse>("/api/v1/end-user/subjects/me/export");
      downloadJson("tiny-hermes-my-data.json", exported);
    } catch (caught) {
      setExportError(problemMessage(caught));
    } finally {
      setExporting(false);
    }
  }

  async function eraseData(): Promise<void> {
    setErasing(true);
    setEraseError(null);
    try {
      const report = await api<ErasureResponse>("/api/v1/end-user/subjects/me/erase", {
        method: "POST",
      });
      setErased(report);
      setConfirmingErase(false);
    } catch (caught) {
      setEraseError(problemMessage(caught));
    } finally {
      setErasing(false);
    }
  }

  return (
    <main className="settings">
      <button type="button" className="settings-back" onClick={() => navigate(-1)}>
        {t("backToChat")}
      </button>
      <h1>{t("settings")}</h1>
      <p className="settings-intro">{t("settingsIntro")}</p>
      <section>
        <h2>{t("appearance")}</h2>
        <div className="choice-pills" role="group" aria-label={t("appearance")}>
          <button
            type="button"
            className={theme.dark ? undefined : "is-on"}
            aria-pressed={!theme.dark}
            onClick={() => theme.setTheme("light")}
          >
            {t("themeLight")}
          </button>
          <button
            type="button"
            className={theme.dark ? "is-on" : undefined}
            aria-pressed={theme.dark}
            onClick={() => theme.setTheme("dark")}
          >
            {t("themeDark")}
          </button>
        </div>
      </section>
      <section>
        <h2>{t("language")}</h2>
        <div className="choice-pills" role="group" aria-label={t("language")}>
          <button
            type="button"
            className={locale === "zh-CN" ? "is-on" : undefined}
            aria-pressed={locale === "zh-CN"}
            onClick={() => setLocale("zh-CN")}
          >
            {t("localeZh")}
          </button>
          <button
            type="button"
            className={locale === "en-US" ? "is-on" : undefined}
            aria-pressed={locale === "en-US"}
            onClick={() => setLocale("en-US")}
          >
            {t("localeEn")}
          </button>
        </div>
      </section>
      <section>
        <h2>{t("exportData")}</h2>
        <p className="settings-hint">{t("exportDataHint")}</p>
        {exportError === null ? null : <p className="auth-error">{exportError}</p>}
        <button type="button" disabled={exporting} onClick={() => void exportData()}>
          {t("exportDataButton")}
        </button>
      </section>
      <section>
        <h2>{t("eraseData")}</h2>
        <p className="settings-hint">{t("eraseDataHint")}</p>
        {eraseError === null ? null : <p className="auth-error">{eraseError}</p>}
        {erased !== null ? <p className="settings-hint">{t("eraseDataDone")}</p> : null}
        {confirmingErase ? (
          <>
            <p className="settings-hint">{t("eraseDataConfirm")}</p>
            <button
              type="button"
              className="is-danger"
              disabled={erasing}
              onClick={() => void eraseData()}
            >
              {t("eraseDataButton")}
            </button>
            <button type="button" onClick={() => setConfirmingErase(false)}>
              {t("cancel")}
            </button>
          </>
        ) : (
          <button type="button" className="is-danger" onClick={() => setConfirmingErase(true)}>
            {t("eraseDataButton")}
          </button>
        )}
      </section>
      <section className="settings-about">
        <h2>{t("about")}</h2>
        <p className="settings-hint">{t("aboutBody")}</p>
        <p className="settings-meta">{t("appVersion")}</p>
      </section>
    </main>
  );
}

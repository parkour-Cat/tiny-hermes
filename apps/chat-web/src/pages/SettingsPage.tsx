import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useChatTheme } from "../theme/ChatTheme";

export function SettingsPage() {
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useChatTheme();

  return (
    <main className="settings">
      <Link to="/" className="settings-back">
        {t("backToChat")}
      </Link>
      <h1>{t("settings")}</h1>
      <p className="settings-intro">{t("settingsIntro")}</p>
      <section>
        <h2>{t("account")}</h2>
        <p className="settings-fact">{auth.user?.display_name}</p>
        <p className="settings-fact">{auth.user?.subject}</p>
      </section>
      <section>
        <h2>{t("appearance")}</h2>
        <div className="choice-row" role="group" aria-label={t("appearance")}>
          <button
            type="button"
            className={theme.dark ? "" : "is-selected"}
            aria-pressed={!theme.dark}
            onClick={() => {
              if (theme.dark) {
                theme.toggle();
              }
            }}
          >
            {t("themeLight")}
          </button>
          <button
            type="button"
            className={theme.dark ? "is-selected" : ""}
            aria-pressed={theme.dark}
            onClick={() => {
              if (!theme.dark) {
                theme.toggle();
              }
            }}
          >
            {t("themeDark")}
          </button>
        </div>
      </section>
      <section>
        <h2>{t("language")}</h2>
        <div className="choice-row" role="group" aria-label={t("language")}>
          <button
            type="button"
            className={locale === "zh-CN" ? "is-selected" : ""}
            aria-pressed={locale === "zh-CN"}
            onClick={() => setLocale("zh-CN")}
          >
            {t("localeZh")}
          </button>
          <button
            type="button"
            className={locale === "en-US" ? "is-selected" : ""}
            aria-pressed={locale === "en-US"}
            onClick={() => setLocale("en-US")}
          >
            {t("localeEn")}
          </button>
        </div>
      </section>
    </main>
  );
}

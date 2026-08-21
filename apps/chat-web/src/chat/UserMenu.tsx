import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useDismiss } from "./useDismiss";
import { useLocale } from "../i18n/locale";
import { useChatTheme } from "../theme/ChatTheme";
import { ChevronDown } from "../ui/ChevronDown";

/**
 * Appearance and language, behind the same corner the console's user card
 * used to occupy — but with no identity to show. Design §4.5.1: the
 * platform holds no name, email, or anything else identifying about an end
 * user by default, so there is nothing here a display name or subject line
 * could read from. What used to be "sign out" is gone with it — ending a
 * session on purpose is an admin's action (design §4.3), not a button this
 * surface can offer its own visitor.
 */
export function UserMenu() {
  const { t, locale, setLocale } = useLocale();
  const theme = useChatTheme();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  return (
    <div className="user-card-wrap" ref={root}>
      <button
        type="button"
        className="rail-user"
        aria-label={t("settings")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="rail-avatar" aria-hidden>
          ⚙
        </span>
        <span>{t("settings")}</span>
        <ChevronDown />
      </button>
      {open ? (
        <div className="user-card" role="dialog" aria-label={t("settings")}>
          <div className="user-card-row">
            <span>{t("appearance")}</span>
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
          </div>
          <div className="user-card-row">
            <span>{t("language")}</span>
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
          </div>
          <div className="user-card-actions">
            <Link to="/settings" onClick={close}>
              {t("settings")}
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  );
}

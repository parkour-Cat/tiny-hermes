import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useDismiss } from "./useDismiss";
import { useAuth } from "../auth/AuthProvider";
import { useLocale } from "../i18n/locale";
import { useChatTheme } from "../theme/ChatTheme";
import { ChevronDown } from "../ui/ChevronDown";

export function UserMenu() {
  const auth = useAuth();
  const { t, locale, setLocale } = useLocale();
  const theme = useChatTheme();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  const name = auth.user?.display_name ?? "";
  const subject = auth.user?.subject ?? "";
  const initial = name.slice(0, 1).toUpperCase() || "?";

  if (auth.user === null) {
    return null;
  }

  return (
    <div className="user-card-wrap" ref={root}>
      <button
        type="button"
        className="rail-user"
        aria-label={name}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="rail-avatar" aria-hidden>
          {initial}
        </span>
        <span>{name}</span>
        <ChevronDown />
      </button>
      {open ? (
        <div className="user-card" role="dialog" aria-label={name}>
          <div className="user-card-head">
            <span className="rail-avatar" aria-hidden>
              {initial}
            </span>
            <div>
              <strong>{name}</strong>
              <p>{subject}</p>
            </div>
          </div>
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
            <button type="button" onClick={() => void auth.logout()}>
              {t("logout")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

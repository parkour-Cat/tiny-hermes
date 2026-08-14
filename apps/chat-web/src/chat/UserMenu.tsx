import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useDismiss } from "./useDismiss";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";

export function UserMenu() {
  const t = useT();
  const auth = useAuth();
  const navigate = useNavigate();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);
  const name = auth.user?.display_name ?? "";
  const initial = name.slice(0, 1).toUpperCase() || "?";

  return (
    <div className="menu-anchor menu-anchor-up" ref={root}>
      <button
        type="button"
        className="rail-user"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="rail-avatar" aria-hidden>
          {initial}
        </span>
        <span>{name}</span>
        <span className="menu-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open ? (
        <div className="menu-panel menu-panel-up" role="menu" aria-label={name}>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate("/settings");
            }}
          >
            {t("settings")}
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void auth.logout();
            }}
          >
            {t("logout")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

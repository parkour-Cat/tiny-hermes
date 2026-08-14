import { useCallback, useEffect, useRef, useState } from "react";

import { useDismiss } from "./useDismiss";
import { useT } from "../i18n/locale";

const CARD_WIDTH = 200;

export function SessionItem({
  session,
  active,
  pinned,
  archived,
  onOpen,
  onPin,
  onArchive,
  onForget,
}: {
  session: { id: string; title: string };
  active: boolean;
  pinned: boolean;
  archived: boolean;
  onOpen: () => void;
  onPin: (pinned: boolean) => void;
  onArchive: (archived: boolean) => void;
  onForget: () => void;
}) {
  const t = useT();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [box, setBox] = useState({ top: 0, left: 0 });
  const close = useCallback(() => {
    setOpen(false);
    setConfirming(false);
  }, []);
  useDismiss(open, close, root);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onScroll(): void {
      close();
    }
    document.addEventListener("scroll", onScroll, true);
    return () => document.removeEventListener("scroll", onScroll, true);
  }, [close, open]);

  return (
    <div
      className={`rail-session${active ? " is-active" : ""}${open ? " is-menu-open" : ""}`}
      ref={root}
    >
      <button
        type="button"
        className="rail-session-open"
        aria-current={active ? "true" : undefined}
        onClick={onOpen}
      >
        {pinned ? <span className="rail-pin" aria-hidden /> : null}
        {session.title}
      </button>
      <button
        type="button"
        className="rail-session-more"
        aria-label={t("sessionActions")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          if (open) {
            close();
            return;
          }
          const rect = event.currentTarget.getBoundingClientRect();
          setBox({
            top: Math.min(rect.top, window.innerHeight - 200),
            left: Math.min(rect.right + 6, window.innerWidth - CARD_WIDTH - 8),
          });
          setConfirming(false);
          setOpen(true);
        }}
      >
        ⋯
      </button>
      {open ? (
        <div
          className="session-card"
          role="dialog"
          aria-label={t("sessionActions")}
          style={{ top: box.top, left: box.left }}
          onMouseDown={(event) => event.stopPropagation()}
        >
          {confirming ? (
            <>
              <p className="session-card-hint">{t("forgetSessionConfirm")}</p>
              <button
                type="button"
                className="is-danger"
                onClick={() => {
                  onForget();
                  close();
                }}
              >
                {t("forgetSessionNow")}
              </button>
              <button type="button" onClick={() => setConfirming(false)}>
                {t("cancel")}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => {
                  onPin(!pinned);
                  close();
                }}
              >
                {pinned ? t("unpinSession") : t("pinSession")}
              </button>
              <button
                type="button"
                onClick={() => {
                  onArchive(!archived);
                  close();
                }}
              >
                {archived ? t("unarchiveSession") : t("archiveSession")}
              </button>
              <button type="button" className="is-danger" onClick={() => setConfirming(true)}>
                {t("forgetSession")}
              </button>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";

import { useDismiss } from "./useDismiss";
import { useT } from "../i18n/locale";

const CARD_WIDTH = 168;

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
  const [box, setBox] = useState({ top: 0, left: 0 });
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onScroll(): void {
      setOpen(false);
    }
    document.addEventListener("scroll", onScroll, true);
    return () => document.removeEventListener("scroll", onScroll, true);
  }, [open]);

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
            setOpen(false);
            return;
          }
          const rect = event.currentTarget.getBoundingClientRect();
          setBox({
            top: Math.min(rect.top, window.innerHeight - 168),
            left: Math.min(rect.right + 6, window.innerWidth - CARD_WIDTH - 8),
          });
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
        >
          <button
            type="button"
            onClick={() => {
              onPin(!pinned);
              setOpen(false);
            }}
          >
            {pinned ? t("unpinSession") : t("pinSession")}
          </button>
          <button
            type="button"
            onClick={() => {
              onArchive(!archived);
              setOpen(false);
            }}
          >
            {archived ? t("unarchiveSession") : t("archiveSession")}
          </button>
          <button
            type="button"
            className="is-danger"
            onClick={() => {
              if (!window.confirm(t("forgetSessionConfirm"))) {
                return;
              }
              onForget();
              setOpen(false);
            }}
          >
            {t("forgetSession")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

import { Modal } from "antd";
import { useContext } from "react";
import { UNSAFE_DataRouterContext, useBeforeUnload, useBlocker } from "react-router-dom";
import { useT } from "../i18n/locale";

type GuardProps = { dirty: boolean; onDiscard?: () => boolean };

function NavigationGuard({ dirty, onDiscard }: GuardProps) {
  const t = useT();
  const blocker = useBlocker(dirty);
  return <Modal open={blocker.state === "blocked"} title={t("unsavedLeaveTitle")}
    okText={t("unsavedDiscard")} cancelText={t("unsavedKeep")}
    onCancel={() => blocker.reset?.()} onOk={() => {
      if (onDiscard?.() !== false) blocker.proceed?.();
      else blocker.reset?.();
    }}>
    {t("unsavedLeaveHint")}
  </Modal>;
}

export function UnsavedChangesGuard({ dirty, onDiscard }: GuardProps) {
  const router = useContext(UNSAFE_DataRouterContext);
  useBeforeUnload((event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  // Standalone editor previews have no data router. The shipped app does, so
  // history navigation and links use the same router-owned cancellation.
  return router ? <NavigationGuard dirty={dirty} onDiscard={onDiscard} /> : null;
}

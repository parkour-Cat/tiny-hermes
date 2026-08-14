import type { ReactNode } from "react";

import { HermesMark } from "./HermesMark";

/**
 * The one empty surface the console uses.
 *
 * Ant Design's default illustration and a Card skeleton are two different
 * claims about the same absence. The listener is the only mark.
 */
export function EmptyState({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="th-empty" role="status">
      <HermesMark size={96} variant="empty" />
      <p className="th-empty-title">{title}</p>
      {action === undefined ? null : <div className="th-empty-action">{action}</div>}
    </div>
  );
}

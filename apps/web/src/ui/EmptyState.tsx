import type { ReactNode } from "react";

/**
 * The one empty surface the console uses.
 *
 * Ant Design's default illustration and a Card skeleton are two different
 * claims about the same absence. This mark is the only one.
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
      <div className="th-empty-mark" aria-hidden="true">
        <span className="th-empty-ring" />
        <span className="th-empty-cut" />
      </div>
      <p className="th-empty-title">{title}</p>
      {action === undefined ? null : <div className="th-empty-action">{action}</div>}
    </div>
  );
}

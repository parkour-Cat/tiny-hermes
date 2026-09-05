import type { ReactNode } from "react";

import { HermesMark } from "./HermesMark";

/**
 * The one empty surface the console uses.
 *
 * Ant Design's default illustration and a Card skeleton are two different
 * claims about the same absence. The listener is the only mark, so an empty
 * list looks like this product's empty list and not like a component demo.
 */
export function EmptyState({ title, action }: { title: ReactNode; action?: ReactNode }) {
  return (
    <div className="th-empty" role="status">
      <HermesMark size={112} variant="empty" />
      <div className="th-empty-title">{title}</div>
      {action === undefined ? null : <div className="th-empty-action">{action}</div>}
    </div>
  );
}

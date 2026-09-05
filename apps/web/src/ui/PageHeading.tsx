import { Typography } from "antd";
import type { ReactNode } from "react";

/**
 * A page's title block: a copper kicker naming where this page sits, the
 * title, one line of intro, and the page's primary action on the right.
 *
 * The kicker is the part that was missing. A title alone says what the page
 * is; the small line above it says what it belongs to, which is the thing a
 * person who arrived by link has to work out before anything else.
 */
export function PageHeading({
  kicker,
  title,
  intro,
  extra,
}: {
  kicker?: string;
  title: string;
  intro?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        {kicker === undefined ? null : <p className="page-kicker">{kicker}</p>}
        <Typography.Title level={2}>{title}</Typography.Title>
        {intro === undefined ? null : (
          <Typography.Paragraph type="secondary">{intro}</Typography.Paragraph>
        )}
      </div>
      {extra}
    </div>
  );
}

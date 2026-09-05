import { Tag } from "antd";

import { useT } from "../i18n/locale";
import { statusLabel } from "../status";

/**
 * One muted tag for every state code. Colour is spent only on the states that
 * change under a person's feet — running, failed, paused — and even those are
 * tints of the paper, not a traffic light: a green pill beside every published
 * Agent made the one row that mattered look like all the others.
 */
export function StatusTag({ code }: { code: string }) {
  const t = useT();
  return <Tag className={`th-tag th-tag-${code}`}>{statusLabel(code, t)}</Tag>;
}

import { Tag } from "antd";

import { useT } from "../i18n/locale";
import { statusLabel } from "../status";

export function StatusTag({ code }: { code: string }) {
  const t = useT();
  return <Tag className={`th-tag th-tag-${code}`}>{statusLabel(code, t)}</Tag>;
}

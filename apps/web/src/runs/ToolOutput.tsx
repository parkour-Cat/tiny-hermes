import { Typography } from "antd";

import { useT } from "../i18n/locale";
import type { ToolRound } from "./transcript";

const LABELS = {
  succeeded: "toolSucceeded",
  failed: "toolFailed",
  pending: "toolPending",
  returned: "toolReturned",
} as const;

export function ToolOutput({ round }: { round: ToolRound }) {
  const t = useT();
  return (
    <div>
      <Typography.Text type={round.status === "failed" ? "danger" : "secondary"}>
        {t(LABELS[round.status])}
        {round.status !== "pending" && round.output.trim() === "" ? ` · ${t("toolNoOutput")}` : ""}
      </Typography.Text>
      {round.output.trim() === "" ? null : (
        <pre className="tool-output">{round.output}</pre>
      )}
    </div>
  );
}

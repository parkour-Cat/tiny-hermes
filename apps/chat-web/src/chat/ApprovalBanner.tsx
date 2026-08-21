import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { decideEndUserApproval } from "../api/approvals";
import { problemMessage } from "../api/messages";
import type { ApprovalResponse } from "../api/types";
import { useT } from "../i18n/locale";

/**
 * Plan §10: the Run in front of this person stopped for their own
 * confirmation, and this is the only place in the app that can answer it —
 * §4.6's matrix names this 仅发起人本人, and this banner only ever shows an
 * approval `useEndUserApprovals` already scoped to this end user's own.
 *
 * Reject asks for a reason before it is enabled, matching the backend's own
 * refusal (`approval_reason_required`) rather than letting the request go
 * and translating the 422 back — a person who has already typed why should
 * not have that discarded by a round trip that was always going to fail.
 */
export function ApprovalBanner({ approval }: { approval: ApprovalResponse }) {
  const t = useT();
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const decide = useMutation({
    mutationFn: (input: { decision: "approve" | "reject"; reason?: string }) =>
      decideEndUserApproval(approval.id, input.decision, input.reason),
    onSuccess: () => {
      setError(null);
      setRejecting(false);
      setReason("");
      void queryClient.invalidateQueries({ queryKey: ["end-user-approvals"] });
      void queryClient.invalidateQueries({ queryKey: ["end-user-run"] });
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  return (
    <div className="banner banner-warn approval-banner">
      <p>{t("approvalPendingTitle")}</p>
      <p className="approval-tool">{approval.tool}</p>
      {error === null ? null : <p className="auth-error">{error}</p>}
      {rejecting ? (
        <div className="approval-reject">
          <textarea
            aria-label={t("approvalReasonPlaceholder")}
            placeholder={t("approvalReasonPlaceholder")}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <div className="approval-actions">
            <button
              type="button"
              className="is-danger"
              disabled={decide.isPending || reason.trim() === ""}
              onClick={() => decide.mutate({ decision: "reject", reason: reason.trim() })}
            >
              {t("approvalReject")}
            </button>
            <button type="button" disabled={decide.isPending} onClick={() => setRejecting(false)}>
              {t("cancel")}
            </button>
          </div>
        </div>
      ) : (
        <div className="approval-actions">
          <button
            type="button"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ decision: "approve" })}
          >
            {t("approvalApprove")}
          </button>
          <button
            type="button"
            className="is-danger"
            disabled={decide.isPending}
            onClick={() => setRejecting(true)}
          >
            {t("approvalReject")}
          </button>
        </div>
      )}
    </div>
  );
}

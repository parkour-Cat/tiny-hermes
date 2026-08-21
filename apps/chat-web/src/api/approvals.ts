import { api } from "./client";
import type { ApprovalResponse } from "./types";

/**
 * Plan §10's other door: what a person needs to see, and answer, their own
 * `user_confirmation`. `GET /api/v1/end-user/approvals` and
 * `POST .../decision` are the routes `end_user_approval_routes.py` opened
 * for this task — see that module's docstring for why the decide route
 * existed before this task and stayed permanently unreachable without a
 * list to find an `approval_id` from.
 */

export function listEndUserApprovals(): Promise<ApprovalResponse[]> {
  return api<ApprovalResponse[]>("/api/v1/end-user/approvals");
}

export function decideEndUserApproval(
  approvalId: string,
  decision: "approve" | "reject",
  reason?: string,
): Promise<ApprovalResponse> {
  return api<ApprovalResponse>(`/api/v1/end-user/approvals/${approvalId}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, reason: reason ?? null }),
  });
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  FileDiffResponse,
  ProposalDetailResponse,
  ProposalResponse,
  SkillVersionResponse,
} from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

const STATUSES: Record<string, MessageKey> = {
  pending: "proposalPending",
  approved: "proposalApproved",
  rejected: "proposalRejected",
};

const CHANGES: Record<string, MessageKey> = {
  added: "proposalFileAdded",
  removed: "proposalFileRemoved",
  changed: "proposalFileChanged",
};

const MARKS: Record<string, string> = {
  added: "+",
  removed: "-",
  context: " ",
  skipped: "…",
};

/**
 * The review queue, and one proposal read whole.
 *
 * §15.3 puts a person here, between a suggestion and a version, so the page's
 * whole job is to make the decision an informed one: the difference is fetched
 * with the proposal rather than on demand, and the approve control is absent —
 * not disabled — for anything the scan blocked, with the findings underneath
 * saying which file stopped it.
 */
export function SkillProposalsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const listQuery = ["skill-proposals", workspaceId] as const;

  const listed = useQuery({
    queryKey: listQuery,
    queryFn: () => api<ProposalResponse[]>("/api/v1/skill-proposals", scope),
    enabled: workspaceId !== null,
  });

  const opened = useQuery({
    queryKey: ["skill-proposal", openId] as const,
    queryFn: () => api<ProposalDetailResponse>(`/api/v1/skill-proposals/${openId ?? ""}`, scope),
    enabled: workspaceId !== null && openId !== null,
  });

  function settled(): void {
    setError(null);
    void queryClient.invalidateQueries({ queryKey: listQuery });
    void queryClient.invalidateQueries({ queryKey: ["skill-proposal"] });
    void queryClient.invalidateQueries({ queryKey: ["skills", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["skill-versions"] });
  }

  const approve = useMutation({
    mutationFn: (proposalId: string) =>
      api<SkillVersionResponse>(`/api/v1/skill-proposals/${proposalId}/approve`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: (version) => {
      setNote(
        t("proposalPublishedNote").replace("{number}", String(version.version_number)),
      );
      settled();
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const reject = useMutation({
    mutationFn: (proposalId: string) =>
      api<ProposalResponse>(`/api/v1/skill-proposals/${proposalId}/reject`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => {
      setNote(null);
      settled();
    },
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  if (listed.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(listed.error, t)}
        action={<Button onClick={() => void listed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const proposals = listed.data ?? [];

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Paragraph type="secondary">{t("proposalsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {note === null ? null : (
        <Alert className="page-alert" type="success" title={note} showIcon />
      )}
      <Card variant="borderless" loading={listed.isPending}>
        {proposals.length === 0 ? (
          <Empty description={t("emptyProposals")} />
        ) : (
          proposals.map((proposal) => (
            <article key={proposal.id} className="workspace-row">
              <div className="workspace-summary">
                <Typography.Title level={4}>{proposal.name}</Typography.Title>
                <Typography.Paragraph type="secondary">
                  {proposal.description}
                </Typography.Paragraph>
                <Space wrap>
                  <Tag>{t(STATUSES[proposal.status] ?? "proposalPending")}</Tag>
                  <Tag>
                    {proposal.origin === "agent"
                      ? t("proposalOriginAgent")
                      : t("proposalOriginHuman")}
                  </Tag>
                  {proposal.skill_id === null ? <Tag>{t("proposalNewSkill")}</Tag> : null}
                  {proposal.origin_run_id === null ? null : (
                    <Link to={`/workspaces/${workspaceId ?? ""}/runs/${proposal.origin_run_id}`}>
                      {t("proposalFromRun")}
                    </Link>
                  )}
                  <Button
                    size="small"
                    onClick={() => setOpenId(openId === proposal.id ? null : proposal.id)}
                  >
                    {t("proposalDiff")}
                  </Button>
                  {/* Absent rather than disabled for anything that cannot be
                      approved: a control that does not move makes the reader
                      guess whether it is their permissions or the content. */}
                  {proposal.approvable ? (
                    <Button
                      type="primary"
                      size="small"
                      loading={approve.isPending}
                      onClick={() => approve.mutate(proposal.id)}
                    >
                      {t("proposalApprove")}
                    </Button>
                  ) : null}
                  {proposal.status === "pending" ? (
                    <Button
                      size="small"
                      loading={reject.isPending}
                      onClick={() =>
                        void modal.confirm({
                          title: t("proposalReject"),
                          content: t("proposalRejectWarning"),
                          okText: t("confirm"),
                          cancelText: t("cancel"),
                          onOk: () => reject.mutateAsync(proposal.id).catch(() => undefined),
                        })
                      }
                    >
                      {t("proposalReject")}
                    </Button>
                  ) : null}
                </Space>
                {proposal.status === "pending" &&
                proposal.findings.some((finding) => finding.severity === "blocking") ? (
                  <Alert
                    className="page-alert"
                    type="error"
                    title={t("proposalBlocked")}
                    description={
                      <ul>
                        {proposal.findings
                          .filter((finding) => finding.severity === "blocking")
                          .map((finding) => (
                            <li key={`${finding.path}-${finding.code}`}>
                              {finding.path}: {finding.detail}
                            </li>
                          ))}
                      </ul>
                    }
                    showIcon
                  />
                ) : null}
                {openId === proposal.id ? (
                  <Diff
                    files={opened.data?.diff ?? []}
                    loading={opened.isPending}
                    error={opened.isError ? problemMessage(opened.error, t) : null}
                  />
                ) : null}
              </div>
            </article>
          ))
        )}
      </Card>
    </>
  );
}

function Diff({
  files,
  loading,
  error,
}: {
  files: FileDiffResponse[];
  loading: boolean;
  error: string | null;
}) {
  const t = useT();
  if (error !== null) {
    return <Alert type="warning" title={error} showIcon />;
  }
  if (loading) {
    return <Typography.Paragraph type="secondary">{t("loading")}</Typography.Paragraph>;
  }
  if (files.length === 0) {
    return <Typography.Paragraph type="secondary">{t("proposalNoDiff")}</Typography.Paragraph>;
  }
  return (
    <div className="skill-diff">
      {files.map((file) => (
        <section key={file.path}>
          <Space wrap>
            <Typography.Text strong>{file.path}</Typography.Text>
            <Tag>{t(CHANGES[file.change] ?? "proposalFileChanged")}</Tag>
            <Typography.Text type="secondary">
              {t("proposalDiffCounts")
                .replace("{added}", String(file.added_lines))
                .replace("{removed}", String(file.removed_lines))}
            </Typography.Text>
          </Space>
          <pre className={`skill-diff-body diff-${file.change}`}>
            {file.lines
              .map((line) => `${MARKS[line.kind] ?? " "} ${line.text}`)
              .join("\n")}
          </pre>
          {file.truncated ? (
            <Typography.Paragraph type="secondary">
              {t("proposalDiffTruncated")}
            </Typography.Paragraph>
          ) : null}
        </section>
      ))}
    </div>
  );
}

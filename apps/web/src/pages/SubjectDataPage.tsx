import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Select, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { MemoryResponse, ResolvedSubjectResponse, SubjectExportResponse } from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { EmptyState } from "../ui/EmptyState";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/**
 * Acting on one person's data-rights request.
 *
 * §4.6 lets a workspace administrator export, correct, forget and erase on
 * a subject's behalf — `依法代办并审计`. Every one of those routes takes the
 * subject's **internal** id, and nothing in this console produced one, so
 * the four of them were reachable only by reading the database by hand.
 *
 * The page starts where a request starts: a person, named the way the
 * enterprise's own directory names them. `channel` + external id resolves
 * to the subject; everything else hangs off that.
 *
 * Partial identifier search is a steward-only, bounded and audited lookup.
 * It does not add an unfiltered user directory.
 */
export function SubjectDataPage() {
  const [params] = useSearchParams();
  const sessionId = params.get("session");
  const t = useT();
  const [searchRequest, setSearchRequest] = useState<{ q: string; channel: string; offset: number } | null>(null);
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const scope = { workspace: workspaceId ?? "" };
  const [channel, setChannel] = useState("web");
  const [externalId, setExternalId] = useState("");
  const [asked, setAsked] = useState<{ channel: string; externalId: string } | null>(null);
  const [correcting, setCorrecting] = useState<MemoryResponse | null>(null);
  const [report, setReport] = useState<Record<string, number> | null>(null);
  const [modal, contextHolder] = Modal.useModal();
  const [form] = Form.useForm<{ body: string }>();

  const candidates = useQuery({
    queryKey: ["subject-search", workspaceId, searchRequest], enabled: workspaceId !== null && searchRequest !== null,
    queryFn: () => api<{ items: ResolvedSubjectResponse[]; has_more: boolean }>(`/api/v1/subjects/search?${new URLSearchParams({ q: searchRequest?.q ?? "", channel: searchRequest?.channel ?? "web", offset: String(searchRequest?.offset ?? 0) })}`, scope),
  });
  const subject = useQuery({
    queryKey: ["subject-lookup", workspaceId, asked, sessionId] as const,
    queryFn: () => {
      if (asked === null && sessionId !== null) return api<ResolvedSubjectResponse>(`/api/v1/subjects/from-session/${encodeURIComponent(sessionId)}`, scope);
      const query = new URLSearchParams({
        channel: asked?.channel ?? "",
        external_user_id: asked?.externalId ?? "",
      });
      return api<ResolvedSubjectResponse>(`/api/v1/subjects/lookup?${query.toString()}`, scope);
    },
    enabled: workspaceId !== null && (asked !== null || sessionId !== null),
  });

  const subjectId = subject.data?.subject_id ?? null;
  const exportQuery = ["subject-export", workspaceId, subjectId] as const;
  const held = useQuery({
    queryKey: exportQuery,
    queryFn: () =>
      api<SubjectExportResponse>(`/api/v1/subjects/${subjectId ?? ""}/export`, scope),
    enabled: subjectId !== null,
  });

  const correct = useMutation({
    mutationFn: (input: { id: string; body: string }) =>
      api<MemoryResponse>(`/api/v1/subjects/memories/${input.id}/correct`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ body: input.body }),
      }),
    onSuccess: () => {
      setCorrecting(null);
      form.resetFields();
      void queryClient.invalidateQueries({ queryKey: exportQuery });
    },
    onError: (caught) => form.setFields([{ name: "body", errors: [problemMessage(caught, t)] }]),
  });

  const forget = useMutation({
    mutationFn: (memoryId: string) =>
      api<MemoryResponse>(`/api/v1/subjects/memories/${memoryId}/forget`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: exportQuery }),
  });

  const erase = useMutation({
    mutationFn: () =>
      api<Record<string, number>>(`/api/v1/subjects/${subjectId ?? ""}/erase`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: (counts) => {
      setReport(counts);
      void queryClient.invalidateQueries({ queryKey: exportQuery });
      void queryClient.invalidateQueries({ queryKey: ["subject-lookup"] });
    },
  });

  const notFound =
    subject.isError && subject.error instanceof ApiError && subject.error.status === 404;
  const memories = held.data?.memories ?? [];
  const erased = subject.data?.erased_at ?? null;

  return (
    <>
      {contextHolder}

      <Card variant="borderless" className="page-alert">
        <Space wrap align="end">
          <Select
            aria-label={t("subjectChannel")}
            style={{ minWidth: 140 }}
            value={channel}
            onChange={setChannel}
            options={[
              { value: "web", label: "web" },
              { value: "feishu", label: "feishu" },
            ]}
          />
          <Input
            aria-label={t("subjectExternalId")}
            placeholder={t("subjectExternalIdHint")}
            style={{ minWidth: 280 }}
            value={externalId}
            onChange={(event) => setExternalId(event.target.value)}
            onPressEnter={() => setAsked({ channel, externalId })}
          />
          <Button disabled={externalId.trim().length < 2} loading={candidates.isFetching} onClick={() => setSearchRequest({ q: externalId.trim(), channel, offset: 0 })}>{t("subjectPartialFind")}</Button>
          <Button
            type="primary"
            loading={subject.isFetching}
            onClick={() => setAsked({ channel, externalId })}
          >
            {t("subjectFind")}
          </Button>
        </Space>
      </Card>

      {searchRequest && <Card className="page-alert" loading={candidates.isPending}>
        <Typography.Paragraph type="secondary">{t("subjectSearchHint")}</Typography.Paragraph>
        {candidates.isError ? <Alert type="error" title={problemMessage(candidates.error, t)} action={<Button onClick={() => void candidates.refetch()}>{t("retry")}</Button>} /> : candidates.data?.items.length === 0 ? <Typography.Paragraph>{t("subjectNotFound")}</Typography.Paragraph> : candidates.data?.items.map((row) => <div key={`${row.channel}:${row.subject_id}`} className="workspace-row"><Tag>{row.channel}</Tag><Button onClick={() => { setChannel(row.channel); setExternalId(row.external_user_id); setAsked({ channel: row.channel, externalId: row.external_user_id }); }}>{row.external_user_id}</Button><Typography.Text type="secondary">{moment(row.first_seen_at)}</Typography.Text></div>)}
        <Space><Button disabled={searchRequest.offset === 0} onClick={() => setSearchRequest({ ...searchRequest, offset: Math.max(0, searchRequest.offset - 20) })}>{t("approvalPreviousPage")}</Button><Button disabled={!candidates.data?.has_more} onClick={() => setSearchRequest({ ...searchRequest, offset: searchRequest.offset + 20 })}>{t("approvalNextPage")}</Button></Space>
      </Card>}

      {notFound ? (
        // Not an empty memory list under their name: that reads as "we hold
        // nothing about this person", which is a different statement.
        <Alert className="page-alert" type="info" showIcon message={t("subjectNotFound")} />
      ) : null}
      {subject.isError && !notFound ? (
        <Alert
          className="page-alert"
          type="warning"
          showIcon
          message={problemMessage(subject.error, t)}
        />
      ) : null}

      {subject.data === undefined ? null : (
        <Card variant="borderless" className="page-alert">
          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2 }}
            items={[
              { key: "external", label: t("subjectExternalId"), children: subject.data.external_user_id },
              {
                key: "id",
                label: t("subjectId"),
                children: <Typography.Text copyable>{subject.data.subject_id}</Typography.Text>,
              },
              { key: "channel", label: t("subjectChannel"), children: <Tag>{subject.data.channel}</Tag> },
              { key: "seen", label: t("subjectFirstSeen"), children: moment(subject.data.first_seen_at) },
              {
                key: "erased",
                label: t("subjectErasedAt"),
                children: erased === null ? t("subjectNotErased") : moment(erased),
              },
              {
                key: "sessions",
                label: t("subjectSessions"),
                children: held.isError || held.data === undefined ? "—" : String(held.data.sessions.length),
              },
            ]}
          />
          {erased === null ? (
            <Space wrap style={{ marginTop: 12 }}>
              <Button
                danger
                loading={erase.isPending}
                onClick={() =>
                  void modal.confirm({
                    title: t("subjectErase"),
                    content: <><Typography.Paragraph>{subject.data?.external_user_id} · {subject.data?.channel} · {workspaceId}</Typography.Paragraph>{t("subjectEraseWarning")}</>,
                    okText: t("confirm"),
                    cancelText: t("cancel"),
                    onOk: () => erase.mutateAsync().catch(() => undefined),
                  })
                }
              >
                {t("subjectErase")}
              </Button>
            </Space>
          ) : (
            // Said rather than left to an empty list: §344 keeps the row, and
            // "already erased, on this date" is what a second request needs.
            <Alert
              style={{ marginTop: 12 }}
              type="warning"
              showIcon
              message={t("subjectAlreadyErased")}
            />
          )}
          {report === null ? null : (
            <Alert
              style={{ marginTop: 12 }}
              type="success"
              showIcon
              message={t("subjectEraseReport")}
              description={`${t("memoryReview")}: ${report.memories} · ${t("subjectSessions")}: ${report.sessions} · ${t("subjectMessages")}: ${report.messages} · ${t("subjectArtifacts")}: ${report.artifacts}`}
            />
          )}
        </Card>
      )}

      {subjectId === null ? null : (
        <Card title={t("subjectMemories")} variant="borderless" loading={held.isPending}>
          {held.isError ? (
            <Alert type="error" showIcon title={problemMessage(held.error, t)} action={<Button onClick={() => void held.refetch()}>{t("retry")}</Button>} />
          ) : memories.length === 0 ? (
            <EmptyState title={t("subjectNoMemories")} />
          ) : (
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              {memories.map((row) => (
                <Card key={row.id} variant="borderless" className="page-alert">
                  <Space direction="vertical" size="small" style={{ width: "100%" }}>
                    <Space wrap>
                      <Tag>{row.kind}</Tag>
                      <Tag>{row.status}</Tag>
                      <Typography.Text type="secondary">{moment(row.updated_at)}</Typography.Text>
                    </Space>
                    <pre className="skill-file-body">{row.body}</pre>
                    <Space wrap>
                      <Button
                        size="small"
                        onClick={() => {
                          setCorrecting(row);
                          form.setFieldsValue({ body: row.body });
                        }}
                      >
                        {t("subjectCorrect")}
                      </Button>
                      <Button
                        size="small"
                        danger
                        loading={forget.isPending}
                        onClick={() => forget.mutate(row.id)}
                      >
                        {t("subjectForget")}
                      </Button>
                    </Space>
                  </Space>
                </Card>
              ))}
            </Space>
          )}
        </Card>
      )}

      <Modal
        open={correcting !== null}
        title={t("subjectCorrect")}
        okText={t("saveName")}
        cancelText={t("cancel")}
        confirmLoading={correct.isPending}
        onCancel={() => setCorrecting(null)}
        onOk={() => void form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) =>
            correcting === null
              ? undefined
              : correct.mutate({ id: correcting.id, body: values.body })
          }
        >
          <Typography.Paragraph type="secondary">{t("subjectCorrectHint")}</Typography.Paragraph>
          <Form.Item name="body" label={t("subjectCorrectedText")} rules={[{ required: true }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

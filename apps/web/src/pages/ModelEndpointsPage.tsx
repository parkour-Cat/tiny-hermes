import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, InputNumber, Select, Space, Tag, Typography , Modal } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  EndpointCheckResponse,
  ModelEndpointDetail,
  ModelEndpointSummary,
} from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";

type RegisterValues = {
  name: string;
  kind: "openai_compatible";
  base_url: string;
  model: string;
  context_window: number;
  max_output_tokens: number;
  usage_quality: "provider" | "unavailable";
  context_accounting: "shared" | "separate";
  tokenizer?: string;
  credential_ref: string;
};

export function ModelEndpointsPage() {
  const t = useT();
  const [modal, contextHolder] = Modal.useModal();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<RegisterValues>();
  const [error, setError] = useState<string | null>(null);
  const [checkNote, setCheckNote] = useState<string | null>(null);
  const admin = auth.user?.is_platform_admin === true;
  const listQuery = ["model-endpoints"] as const;

  const listed = useQuery({
    queryKey: listQuery,
    queryFn: () => api<ModelEndpointSummary[]>("/api/v1/model-endpoints"),
  });

  const details = useQuery({
    queryKey: ["model-endpoint-details", listed.data?.map((entry) => entry.id)] as const,
    queryFn: async () => {
      const rows = await Promise.all(
        (listed.data ?? []).map((entry) =>
          api<ModelEndpointDetail>(`/api/v1/model-endpoints/${entry.id}`),
        ),
      );
      return Object.fromEntries(rows.map((row) => [row.id, row]));
    },
    enabled: admin && listed.data !== undefined && listed.data.length > 0,
  });

  const register = useMutation({
    mutationFn: (values: RegisterValues) =>
      api<ModelEndpointDetail>("/api/v1/model-endpoints", {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: listQuery });
      form.resetFields();
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "disabled" }) =>
      api<ModelEndpointDetail>(`/api/v1/model-endpoints/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: listQuery }),
    onError: (caught) => setError(problemMessage(caught)),
  });

  const check = useMutation({
    mutationFn: (id: string) =>
      api<EndpointCheckResponse>(`/api/v1/model-endpoints/${id}/check`, { method: "POST" }),
    onSuccess: (result) => {
      setCheckNote(
        result.reachable
          ? `${t("endpointReachable")} (${result.elapsed_ms}ms)`
          : `${t("endpointUnreachable")}${result.detail === null || result.detail === undefined ? "" : `: ${result.detail}`}`,
      );
      setError(null);
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (listed.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(listed.error)}
        action={<Button onClick={() => void listed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("endpointsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("endpointsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {checkNote === null ? null : (
        <Alert className="page-alert" type="info" title={checkNote} showIcon />
      )}
      {admin ? (
        <Card title={t("registerEndpoint")} variant="borderless" className="page-alert">
          <Form<RegisterValues>
            form={form}
            layout="vertical"
            requiredMark={false}
            initialValues={{
              kind: "openai_compatible",
              usage_quality: "unavailable",
              context_accounting: "shared",
              context_window: 128000,
              max_output_tokens: 4096,
            }}
            onFinish={(values) => register.mutate(values)}
          >
            <Form.Item
              name="name"
              label={t("endpointName")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input />
            </Form.Item>
            <Form.Item name="kind" hidden>
              <Input />
            </Form.Item>
            <Form.Item
              name="base_url"
              label={t("endpointBaseUrl")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="model"
              label={t("endpointModel")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="context_window"
              label={t("endpointContextWindow")}
              rules={[{ required: true, message: t("required") }]}
            >
              <InputNumber min={1} className="full-width" />
            </Form.Item>
            <Form.Item
              name="max_output_tokens"
              label={t("endpointMaxOutput")}
              rules={[{ required: true, message: t("required") }]}
            >
              <InputNumber min={1} className="full-width" />
            </Form.Item>
            <Form.Item name="usage_quality" label={t("endpointUsageQuality")}>
              <Select
                options={[
                  { value: "provider", label: "provider" },
                  { value: "unavailable", label: "unavailable" },
                ]}
              />
            </Form.Item>
            <Form.Item name="context_accounting" label={t("endpointContextAccounting")}>
              <Select
                options={[
                  { value: "shared", label: t("endpointAccountingShared") },
                  { value: "separate", label: t("endpointAccountingSeparate") },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="tokenizer"
              label={t("endpointTokenizer")}
              extra={t("endpointTokenizerNote")}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="credential_ref"
              label={t("endpointCredentialRef")}
              rules={[{ required: true, message: t("required") }]}
            >
              <Input />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={register.isPending}>
              {t("registerEndpoint")}
            </Button>
          </Form>
        </Card>
      ) : null}
      <Card loading={listed.isPending} variant="borderless">
        {(listed.data ?? []).length === 0 ? (
          <Empty description={t("emptyEndpoints")} />
        ) : (
          (listed.data ?? []).map((entry) => {
            const detail = details.data?.[entry.id];
            return (
              <article key={entry.id} className="workspace-row">
                <div className="workspace-summary">
                  <Typography.Title level={4}>{entry.name}</Typography.Title>
                  <Typography.Paragraph type="secondary">{entry.model}</Typography.Paragraph>
                  <Typography.Paragraph type="secondary">
                    {t("endpointWindowSummary")
                      .replace("{window}", String(entry.context_window))
                      .replace("{output}", String(entry.max_output_tokens))
                      .replace(
                        "{accounting}",
                        entry.context_accounting === "separate"
                          ? t("endpointAccountingSeparate")
                          : t("endpointAccountingShared"),
                      )}
                  </Typography.Paragraph>
                  {detail === undefined ? null : (
                    <>
                      <Typography.Paragraph type="secondary">{detail.base_url}</Typography.Paragraph>
                      <Typography.Text>
                        {detail.credential_available
                          ? t("credentialAvailable")
                          : t("credentialMissing")}
                      </Typography.Text>
                    </>
                  )}
                </div>
                <Space wrap>
                  <Tag>{entry.status}</Tag>
                  {admin ? (
                    <>
                      <Button onClick={() => check.mutate(entry.id)} loading={check.isPending}>
                        {t("checkEndpoint")}
                      </Button>
                      {entry.status === "active" ? (
                        <Button
                          onClick={() =>
                            void modal.confirm({
                              title: t("disableEndpoint"),
                              content: t("disableEndpointWarning"),
                              okText: t("confirm"),
                              cancelText: t("cancel"),
                              onOk: () => setStatus.mutateAsync({ id: entry.id, status: "disabled" }).catch(() => undefined),
                            })
                          }
                        >
                          {t("disableEndpoint")}
                        </Button>
                      ) : (
                        <Button
                          onClick={() => setStatus.mutate({ id: entry.id, status: "active" })}
                        >
                          {t("enableEndpoint")}
                        </Button>
                      )}
                    </>
                  ) : null}
                </Space>
              </article>
            );
          })
        )}
      </Card>
    </>
  );
}

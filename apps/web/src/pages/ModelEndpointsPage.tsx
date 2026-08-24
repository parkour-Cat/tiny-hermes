import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, InputNumber, Select, Space, Tag, Typography , Modal } from "antd";
import { useState } from "react";

import { ApiError, api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  EndpointCheckResponse,
  ModelEndpointDetail,
  ModelEndpointSummary,
  PricingVersionResponse,
  SecretResponse,
} from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
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

  // The credential is picked from what is stored, never typed. The channel
  // binding form already argued why: a free-text reference is how you point
  // at a secret that does not exist and find out hours later, inside a call
  // nobody is watching. This field was the console's last free-text one.
  // `/api/v1/secrets` is workspace-scoped — it calls `require_workspace_id`
  // and refuses without the header. This page is otherwise platform-level,
  // so the header is easy to forget; forgetting it produced an empty
  // dropdown and no error a person could see.
  const workspaceId = useWorkspaceId();
  const secrets = useQuery({
    queryKey: ["secrets", workspaceId] as const,
    queryFn: () =>
      api<SecretResponse[]>("/api/v1/secrets", { workspace: workspaceId ?? "" }),
    enabled: admin && workspaceId !== null,
  });

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

  // §6's other half. Usage is money, and until this the price a Run is
  // measured against lived only in the database — the Usage page could show
  // what was spent without anything showing the rate it was spent at.
  const prices = useQuery({
    queryKey: ["endpoint-pricing", listed.data?.map((entry) => entry.id)] as const,
    queryFn: async () => {
      const rows = await Promise.all(
        (listed.data ?? []).map(async (entry) => {
          try {
            return [
              entry.id,
              await api<PricingVersionResponse>(`/api/v1/model-endpoints/${entry.id}/pricing`),
            ] as const;
          } catch (caught) {
            // A 404 is "no price set", which is a state rather than a
            // failure — and a different one from "priced at nothing".
            if (caught instanceof ApiError && caught.status === 404) {
              return [entry.id, null] as const;
            }
            throw caught;
          }
        }),
      );
      return Object.fromEntries(rows);
    },
    enabled: admin && listed.data !== undefined && listed.data.length > 0,
  });

  const [pricingFor, setPricingFor] = useState<string | null>(null);
  const [priceForm] = Form.useForm<{
    currency: string;
    inputPerMillion: string;
    outputPerMillion: string;
  }>();

  const setPrice = useMutation({
    mutationFn: (values: {
      id: string;
      currency: string;
      inputPerMillion: string;
      outputPerMillion: string;
    }) =>
      api<PricingVersionResponse>(`/api/v1/model-endpoints/${values.id}/pricing`, {
        method: "POST",
        // Decimal strings, never numbers. Money through a float is how a
        // rate becomes 3.0000000000000004; the API takes text on purpose
        // and this must not undo it.
        body: JSON.stringify({
          currency: values.currency,
          input_per_million: values.inputPerMillion,
          output_per_million: values.outputPerMillion,
        }),
      }),
    onSuccess: () => {
      setPricingFor(null);
      priceForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ["endpoint-pricing"] });
    },
    onError: (caught) =>
      priceForm.setFields([{ name: "inputPerMillion", errors: [problemMessage(caught, t)] }]),
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
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "disabled" }) =>
      api<ModelEndpointDetail>(`/api/v1/model-endpoints/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: listQuery }),
    onError: (caught) => setError(problemMessage(caught, t)),
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
              extra={t("endpointCredentialRefHint")}
              rules={[{ required: true, message: t("required") }]}
            >
              {/* Shows the name a person recognises and submits the id the
                  resolver accepts. Both scopes are offered because
                  `CredentialResolver` looks a Secret up by id and never asks
                  which scope it came from — filtering here would hide
                  credentials that work. */}
              <Select
                options={(secrets.data ?? [])
                  .filter((secret) => secret.status === "active")
                  .map((secret) => ({
                    value: secret.id,
                    label: `${secret.name} · ${secret.scope}`,
                  }))}
              />
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
                  {admin ? (
                    <Typography.Paragraph type="secondary">
                      {(prices.data ?? {})[entry.id] == null
                        ? // Said, not shown as a zero: "priced at nothing"
                          // and "not priced" are different states, and a
                          // zero for the second makes every Run look free.
                          t("pricingUnset")
                        : `${(prices.data ?? {})[entry.id]?.currency} ${(prices.data ?? {})[entry.id]?.input_per_million} / ${(prices.data ?? {})[entry.id]?.output_per_million} ${t("pricingPerMillion")}`}
                    </Typography.Paragraph>
                  ) : null}
                </div>
                <Space wrap>
                  <Tag>{entry.status}</Tag>
                  {admin ? (
                    <>
                      <Button onClick={() => check.mutate(entry.id)} loading={check.isPending}>
                        {t("checkEndpoint")}
                      </Button>
                      <Button
                        onClick={() => {
                          setPricingFor(entry.id);
                          priceForm.setFieldsValue({ currency: "USD" });
                        }}
                      >
                        {t("setPricing")}
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
      <Modal
        open={pricingFor !== null}
        title={t("setPricing")}
        okText={t("saveName")}
        cancelText={t("cancel")}
        confirmLoading={setPrice.isPending}
        onCancel={() => setPricingFor(null)}
        onOk={() => void priceForm.submit()}
      >
        <Form
          form={priceForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) =>
            pricingFor === null ? undefined : setPrice.mutate({ id: pricingFor, ...values })
          }
        >
          <Typography.Paragraph type="secondary">{t("pricingHint")}</Typography.Paragraph>
          <Form.Item name="currency" label={t("pricingCurrency")} rules={[{ required: true }]}>
            <Input maxLength={3} />
          </Form.Item>
          {/* Text inputs, not `InputNumber`: a rate typed as 3.00 must reach
              the API as "3.00". A numeric control hands back a float, and a
              float is how a price becomes 3.0000000000000004. */}
          <Form.Item name="inputPerMillion" label={t("pricingInput")} rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
          <Form.Item name="outputPerMillion" label={t("pricingOutput")} rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

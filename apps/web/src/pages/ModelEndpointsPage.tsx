import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Switch, Tag, Typography } from "antd";
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
import { FormSection } from "../forms/FormSection";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useT } from "../i18n/locale";
import { EmptyState } from "../ui/EmptyState";

/** One form for both registering and editing, in three sections: 连到哪、
 *  这个模型的能力、计价. The pricing fields are optional at registration
 *  and become a second request (`POST …/pricing`) when filled. */
type EndpointValues = {
  name: string;
  kind: "openai_compatible";
  base_url: string;
  model: string;
  context_window: number;
  max_output_tokens: number;
  usage_quality: "provider" | "unavailable";
  context_accounting: "shared" | "separate";
  accepts_images: boolean;
  tokenizer?: string;
  credential_ref: string;
  currency: string;
  inputPerMillion?: string;
  outputPerMillion?: string;
};

const DEFAULTS = {
  kind: "openai_compatible",
  usage_quality: "unavailable",
  context_accounting: "shared",
  accepts_images: false,
  // No default window: a guessed 128000 that is silently wrong for this
  // model is worse than an empty required field, and the empty field is
  // what keeps 「这个模型的能力」 open on a new form (§5.5).
  currency: "USD",
} as const;

function hasPrice(values: Pick<EndpointValues, "inputPerMillion" | "outputPerMillion">): boolean {
  return Boolean(values.inputPerMillion) && Boolean(values.outputPerMillion);
}

export function ModelEndpointsPage() {
  const t = useT();
  const [modal, contextHolder] = Modal.useModal();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<EndpointValues>();
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

  // Decimal strings, never numbers. Money through a float is how a rate
  // becomes 3.0000000000000004; the API takes text on purpose and this must
  // not undo it.
  const postPrice = (id: string, values: EndpointValues) =>
    api<PricingVersionResponse>(`/api/v1/model-endpoints/${id}/pricing`, {
      method: "POST",
      body: JSON.stringify({
        currency: values.currency,
        input_per_million: values.inputPerMillion,
        output_per_million: values.outputPerMillion,
      }),
    });

  const [editing, setEditing] = useState<ModelEndpointSummary | null>(null);

  const finish = () => {
    setEditing(null);
    form.resetFields();
    setError(null);
    void queryClient.invalidateQueries({ queryKey: listQuery });
    void queryClient.invalidateQueries({ queryKey: ["endpoint-pricing"] });
  };

  const register = useMutation({
    mutationFn: async (values: EndpointValues) => {
      const created = await api<ModelEndpointDetail>("/api/v1/model-endpoints", {
        method: "POST",
        body: JSON.stringify({
          name: values.name,
          kind: values.kind,
          base_url: values.base_url,
          model: values.model,
          context_window: values.context_window,
          max_output_tokens: values.max_output_tokens,
          usage_quality: values.usage_quality,
          context_accounting: values.context_accounting,
          accepts_images: values.accepts_images,
          tokenizer: values.tokenizer,
          credential_ref: values.credential_ref,
        }),
      });
      // The price is its own resource with its own history, so it is a
      // second request — made only when both rates were typed. An endpoint
      // may be registered unpriced; the list then says so in words.
      if (hasPrice(values)) await postPrice(created.id, values);
      return created;
    },
    onSuccess: finish,
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  // Edit sends only what changed, and never `status`: a PATCH naming it
  // beside a capability would disable the endpoint as a side effect of
  // correcting the capability. Connection fields are not amendable at all —
  // changing model or address swaps the endpoint underneath every
  // AgentVersion that named it — so the form shows them disabled.
  const amend = useMutation({
    mutationFn: async ({ entry, values }: { entry: ModelEndpointSummary; values: EndpointValues }) => {
      const patch: Record<string, unknown> = {};
      if (
        values.context_window !== entry.context_window ||
        values.max_output_tokens !== entry.max_output_tokens
      ) {
        patch.context_window = values.context_window;
        patch.max_output_tokens = values.max_output_tokens;
      }
      if (values.accepts_images !== entry.accepts_images) patch.accepts_images = values.accepts_images;
      if (Object.keys(patch).length > 0) {
        await api<ModelEndpointSummary>(`/api/v1/model-endpoints/${entry.id}`, {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
      }
      const current = (prices.data ?? {})[entry.id] ?? null;
      const priceChanged =
        current === null ||
        current.currency !== values.currency ||
        current.input_per_million !== values.inputPerMillion ||
        current.output_per_million !== values.outputPerMillion;
      if (hasPrice(values) && priceChanged) await postPrice(entry.id, values);
    },
    onSuccess: finish,
    onError: (caught) => setError(problemMessage(caught, t)),
  });

  const startEditing = (entry: ModelEndpointSummary) => {
    const price = (prices.data ?? {})[entry.id] ?? null;
    setEditing(entry);
    setError(null);
    form.setFieldsValue({
      name: entry.name,
      kind: "openai_compatible",
      base_url: details.data?.[entry.id]?.base_url ?? "",
      model: entry.model,
      context_window: entry.context_window,
      max_output_tokens: entry.max_output_tokens,
      usage_quality: entry.usage_quality as EndpointValues["usage_quality"],
      context_accounting: entry.context_accounting as EndpointValues["context_accounting"],
      accepts_images: entry.accepts_images ?? false,
      tokenizer: entry.tokenizer ?? "",
      credential_ref: "",
      currency: price?.currency ?? "USD",
      inputPerMillion: price?.input_per_million ?? "",
      outputPerMillion: price?.output_per_million ?? "",
    });
  };

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

  // What the folded sections say. Read from the form so the bar tracks the
  // typing, not the last saved state.
  const watchedWindow = Form.useWatch("context_window", form) as number | undefined;
  const watchedOutput = Form.useWatch("max_output_tokens", form) as number | undefined;
  const watchedImages = Form.useWatch("accepts_images", form) as boolean | undefined;
  const watchedCurrency = Form.useWatch("currency", form) as string | undefined;
  const watchedInput = Form.useWatch("inputPerMillion", form) as string | undefined;
  const watchedOutputPrice = Form.useWatch("outputPerMillion", form) as string | undefined;
  const capabilitySummary = `${watchedWindow ?? "—"} ${t("endpointWindowUnit")} · ${t("endpointOutputPrefix")} ${watchedOutput ?? "—"} · ${watchedImages ? t("endpointTakesImages") : t("endpointNoImages")}`;
  const pricingSummary =
    watchedInput && watchedOutputPrice
      ? `${watchedCurrency ?? ""} ${watchedInput} / ${watchedOutputPrice} ${t("pricingPerMillion")}`
      : t("pricingUnsetSummary");

  const connectionRules =
    editing === null ? [{ required: true, message: t("required") }] : [];

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
        <Card
          title={editing === null ? t("registerEndpoint") : `${t("edit")}：${editing.name}`}
          variant="borderless"
          className="page-alert"
        >
          <Form<EndpointValues>
            form={form}
            layout="vertical"
            requiredMark={false}
            initialValues={DEFAULTS}
            onFinish={(values) =>
              editing === null ? register.mutate(values) : amend.mutate({ entry: editing, values })
            }
          >
            {/* 连到哪：全是必填，且注册之后不可改，所以从不折叠。 */}
            {/* 连到哪：注册时全是必填；编辑时只读，因为改模型或地址等于把每个
                引用它的 AgentVersion 底下的端点换掉。 */}
            <FormSection
              title={t("endpointSectionConnection")}
              summary=""
              fields={["name", "base_url", "model", "credential_ref"]}
              collapsible={false}
            >
              <Form.Item
                name="name"
                label={t("endpointName")}
                rules={connectionRules}
              >
                <Input disabled={editing !== null} />
              </Form.Item>
              <Form.Item name="kind" hidden>
                <Input />
              </Form.Item>
              <Form.Item
                name="base_url"
                label={t("endpointBaseUrl")}
                rules={connectionRules}
              >
                <Input disabled={editing !== null} />
              </Form.Item>
              <Form.Item
                name="model"
                label={t("endpointModel")}
                rules={connectionRules}
              >
                <Input disabled={editing !== null} />
              </Form.Item>
              {editing === null ? (
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
              ) : (
                // The credential's id is never read back, so there is nothing
                // to show and nothing to change here; the list says whether
                // one is available.
                <Form.Item name="credential_ref" hidden>
                  <Input />
                </Form.Item>
              )}
            </FormSection>
            {/* Keyed on what is being edited: switching between 新建 and an
                endpoint remounts the fold, so an edit opens folded on its
                current values instead of inheriting the new form's open state. */}
            <FormSection
              key={`capability-${editing?.id ?? "new"}`}
              title={t("endpointSectionCapability")}
              summary={capabilitySummary}
              fields={["context_window", "max_output_tokens"]}
              collapsible
            >
              {editing === null ? null : (
                <Typography.Paragraph type="secondary">{t("adjustWindowHint")}</Typography.Paragraph>
              )}
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
                  disabled={editing !== null}
                  options={[
                    { value: "provider", label: "provider" },
                    { value: "unavailable", label: "unavailable" },
                  ]}
                />
              </Form.Item>
              <Form.Item name="context_accounting" label={t("endpointContextAccounting")}>
                <Select
                  disabled={editing !== null}
                  options={[
                    { value: "shared", label: t("endpointAccountingShared") },
                    { value: "separate", label: t("endpointAccountingSeparate") },
                  ]}
                />
              </Form.Item>
              <Form.Item
                name="accepts_images"
                label={t("endpointAcceptsImages")}
                extra={t("endpointAcceptsImagesNote")}
                valuePropName="checked"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="tokenizer"
                label={t("endpointTokenizer")}
                extra={t("endpointTokenizerNote")}
              >
                <Input disabled={editing !== null} />
              </Form.Item>
            </FormSection>
            <FormSection
              key={`pricing-${editing?.id ?? "new"}`}
              title={t("endpointSectionPricing")}
              summary={pricingSummary}
              fields={[]}
              collapsible
            >
              <Typography.Paragraph type="secondary">{t("pricingHint")}</Typography.Paragraph>
              <Form.Item name="currency" label={t("pricingCurrency")}>
                <Input maxLength={3} />
              </Form.Item>
              {/* Text inputs, not `InputNumber`: a rate typed as 3.00 must reach
                  the API as "3.00". A numeric control hands back a float, and a
                  float is how a price becomes 3.0000000000000004. */}
              <Form.Item name="inputPerMillion" label={t("pricingInput")}>
                <Input inputMode="decimal" />
              </Form.Item>
              <Form.Item name="outputPerMillion" label={t("pricingOutput")}>
                <Input inputMode="decimal" />
              </Form.Item>
            </FormSection>
            <Space>
              <Button
                type="primary"
                htmlType="submit"
                loading={register.isPending || amend.isPending}
              >
                {editing === null ? t("registerEndpoint") : t("saveName")}
              </Button>
              {editing === null ? null : (
                <Button
                  onClick={() => {
                    setEditing(null);
                    form.resetFields();
                  }}
                >
                  {t("cancel")}
                </Button>
              )}
            </Space>
          </Form>
        </Card>
      ) : null}
      <Card loading={listed.isPending} variant="borderless">
        {(listed.data ?? []).length === 0 ? (
          <EmptyState title={t("emptyEndpoints")} />
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
                      <Button onClick={() => startEditing(entry)}>{t("edit")}</Button>
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

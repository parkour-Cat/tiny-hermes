import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentResponse,
  ChannelBindingResponse,
  ChannelIssuerResponse,
  SecretResponse,
} from "../api/types";
import { moment } from "../i18n/moment";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

/**
 * §20.1's Channels, which had nothing behind it.
 *
 * Every other piece of the Feishu transport shipped and worked — signature
 * verification, decryption, the exactly-once claim, ingestion into a Run —
 * and the only thing that had ever written a `channel_bindings` row was a
 * test. The whole channel was reachable by inserting a row into Postgres by
 * hand and no other way.
 *
 * The key is named, never typed. §4.6 lets an administrator manage this
 * metadata `不查看明文`, and a field that took the encrypt key itself would
 * put it in a request body and in this page's memory — which is the exact
 * thing migration 0037 restructured the table to avoid.
 */
export function ChannelsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const scope = { workspace: workspaceId ?? "" };
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<{ agentId: string; encryptKeyRef: string; appId: string }>();
  // Which binding the edit dialog is open on, or null. The row rather than
  // its id, because the dialog seeds its fields from the current values —
  // an edit form that started empty would look like it was about to clear
  // everything it did not mention.
  const [editing, setEditing] = useState<ChannelBindingResponse | null>(null);
  const [editForm] = Form.useForm<{
    appId: string;
    encryptKeyRef: string;
    appSecretRef?: string;
    transport: string;
  }>();
  // Set once a PATCH actually changed `transport`, and left up rather than a
  // toast: the platform does not hot-reload transports (Task 4's deliberate
  // choice), so a person who switched this and stopped looking at the screen
  // a second later must still find the warning there.
  const [transportRestartHint, setTransportRestartHint] = useState(false);


  const bindingsQuery = ["channel-bindings", workspaceId] as const;
  const bindings = useQuery({
    queryKey: bindingsQuery,
    queryFn: () => api<ChannelBindingResponse[]>("/api/v1/channel-bindings", scope),
    enabled: workspaceId !== null,
  });
  const agents = useQuery({
    queryKey: ["agents", workspaceId] as const,
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope),
    enabled: workspaceId !== null,
  });
  const nothingBound = (bindings.data ?? []).length === 0 && !bindings.isPending;
  // While the dialog is open, or while nothing is bound yet. Those are the
  // two moments the answer matters — somebody about to bind, and somebody
  // who has never bound anything — and a page listing live channels should
  // not pull a secrets listing every visit to say something already known.
  const secrets = useQuery({
    queryKey: ["secrets", workspaceId] as const,
    queryFn: () => api<SecretResponse[]>("/api/v1/secrets", scope),
    enabled: workspaceId !== null && (open || editing !== null || nothingBound),
  });

  // A binding says which Agent is published; an issuer says whose word this
  // platform takes for who a person is. Neither is usable without the other,
  // so they belong on one page rather than in two nav entries.
  const issuersQuery = ["channel-issuers", workspaceId] as const;
  const issuers = useQuery({
    queryKey: issuersQuery,
    queryFn: () => api<ChannelIssuerResponse[]>("/api/v1/channel-issuers", scope),
    enabled: workspaceId !== null,
  });
  const [registering, setRegistering] = useState(false);
  const [issuerForm] =
    Form.useForm<{ issuer: string; keyMode: "jwks" | "publicKey"; jwksUrl?: string; publicKey?: string; origins: string }>();
  //: Which half of the form is showing. The API takes one verification
  //: material or the other, never both, so the choice has to be made here
  //: rather than by leaving the unused field blank and sending it anyway.
  const issuerKeyMode = Form.useWatch("keyMode", issuerForm) ?? "jwks";

  const registerIssuer = useMutation({
    mutationFn: (values: {
      issuer: string;
      keyMode: "jwks" | "publicKey";
      jwksUrl?: string;
      publicKey?: string;
      origins: string;
    }) =>
      api<ChannelIssuerResponse>("/api/v1/channel-issuers", {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          channel: "web",
          issuer: values.issuer,
          // Only the chosen one. Sending the other as `""` would store a
          // second verification material the platform then tries to use —
          // a JWKS fetch against an empty URL, or a key that verifies
          // nothing — instead of the one the author actually supplied.
          ...(values.keyMode === "publicKey"
            ? { public_key: values.publicKey }
            : { jwks_url: values.jwksUrl }),
          // One per line. A comma-separated blob would split an origin that
          // legitimately contains one, and the API takes a list anyway.
          allowed_origins: values.origins
            .split("\n")
            .map((line) => line.trim())
            .filter((line) => line !== ""),
        }),
      }),
    onSuccess: () => {
      setRegistering(false);
      issuerForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: issuersQuery });
    },
    onError: (caught) =>
      issuerForm.setFields([{ name: "issuer", errors: [problemMessage(caught, t)] }]),
  });

  const disableIssuer = useMutation({
    mutationFn: (issuerId: string) =>
      api<ChannelIssuerResponse>(`/api/v1/channel-issuers/${issuerId}/disable`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: issuersQuery }),
  });

  const bind = useMutation({
    mutationFn: (values: {
      agentId: string;
      encryptKeyRef: string;
      appId: string;
      appSecretRef?: string;
    }) =>
      api<ChannelBindingResponse>("/api/v1/channel-bindings", {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          channel: "feishu",
          agent_id: values.agentId,
          app_id: values.appId,
          encrypt_key_ref: values.encryptKeyRef,
          // Omitted, not sent as null, when unset: a binding with no app
          // secret is receive-only (§929's drill), and the key's absence is
          // what says so.
          ...(values.appSecretRef ? { app_secret_ref: values.appSecretRef } : {}),
        }),
      }),
    onSuccess: () => {
      setOpen(false);
      form.resetFields();
      void queryClient.invalidateQueries({ queryKey: bindingsQuery });
    },
    // Into the dialog, which stays open with the typed values in it: a
    // refused app id or an already-bound Agent is something to edit.
    onError: (caught) =>
      form.setFields([{ name: "appId", errors: [problemMessage(caught, t)] }]),
  });

  const disable = useMutation({
    mutationFn: (bindingId: string) =>
      api<ChannelBindingResponse>(`/api/v1/channel-bindings/${bindingId}/disable`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: bindingsQuery }),
  });

  const rewire = useMutation({
    mutationFn: (values: {
      appId: string;
      encryptKeyRef: string;
      appSecretRef?: string;
      transport: string;
    }) => {
      const current = editing;
      if (current === null) throw new Error("no binding is open");
      // Only what actually changed. Resubmitting the whole form would send
      // `encrypt_key_ref` on every edit, and the API reads an explicit
      // `null` as "clear" — so a form field the user never touched could
      // strip the key that makes inbound work.
      const changes: Record<string, string | null> = {};
      if (values.appId !== (current.app_id ?? "")) changes.app_id = values.appId || null;
      if (values.encryptKeyRef !== current.encrypt_key_ref) {
        changes.encrypt_key_ref = values.encryptKeyRef;
      }
      if ((values.appSecretRef ?? null) !== current.app_secret_ref) {
        changes.app_secret_ref = values.appSecretRef ?? null;
      }
      if (values.transport !== current.transport) {
        changes.transport = values.transport;
      }
      return api<ChannelBindingResponse>(`/api/v1/channel-bindings/${current.id}`, {
        ...scope,
        method: "PATCH",
        body: JSON.stringify(changes),
      });
    },
    onSuccess: (updated) => {
      // Compared against the binding the dialog was opened on, not against
      // the form's own dirty-tracking: `editing` is still that pre-update
      // row here, one line above where it gets replaced.
      if (editing !== null && updated.transport !== editing.transport) {
        setTransportRestartHint(true);
      }
      setEditing(null);
      editForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: bindingsQuery });
    },
    onError: (caught) =>
      editForm.setFields([{ name: "appSecretRef", errors: [problemMessage(caught, t)] }]),
  });

  if (bindings.isError) {
    // §4.6 gives a viewer `否` here, so a refusal is an ordinary outcome on
    // this page rather than a fault. An empty table would tell them this
    // workspace publishes nothing, which is a different and false statement.
    return (
      <Alert
        type="warning"
        showIcon
        message={problemMessage(bindings.error, t)}
        description={t("channelsForbiddenHint")}
      />
    );
  }

  const rows = bindings.data ?? [];
  const named = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));
  const usable = (secrets.data ?? []).filter(
    (secret) => secret.status === "active" && secret.scope === "workspace",
  );
  // Read off the binding as **stored**, not off what is currently typed in
  // the dialog: an administrator who adds an app secret and switches
  // transport in one save still has to save twice, because this does not
  // track the unsaved fields. The API validates the resulting binding
  // either way — this control only keeps the common case from having to be
  // refused to learn why. `channelTransportNeedsCredentials` is where that
  // second save is spelled out, because a disabled option that does not
  // un-disable when you fill the field it names looks broken otherwise.
  const canHoldLongConnection =
    editing !== null && editing.app_id !== null && editing.app_secret_ref !== null;

  return (
    <>
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("channels")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("channelsIntro")}</Typography.Paragraph>
        </div>
        <Button type="primary" onClick={() => setOpen(true)}>
          {t("bindChannel")}
        </Button>
      </div>

      {transportRestartHint ? (
        // Left up until dismissed, not a toast: the scheduler does not
        // hot-reload transports, so the moment this matters is after the
        // dialog has already closed and the person has moved on.
        <Alert
          type="warning"
          showIcon
          closable
          className="page-alert"
          message={t("channelTransportRestartHint")}
          onClose={() => setTransportRestartHint(false)}
        />
      ) : null}

      <Card loading={bindings.isPending} variant="borderless">
        {rows.length === 0 ? (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Empty description={t("channelsEmpty")} />
            {usable.length === 0 && !secrets.isPending ? (
              // The first thing to do, said where somebody who has bound
              // nothing is actually standing.
              <Alert type="info" showIcon message={t("channelsNeedSecret")} />
            ) : null}
          </Space>
        ) : (
          <Table<ChannelBindingResponse>
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={rows}
            columns={[
              { title: t("channelKind"), dataIndex: "channel", render: (v: string) => <Tag>{v}</Tag> },
              {
                title: t("channelAgent"),
                dataIndex: "agent_id",
                // By name, because "which Agent did we publish" is the
                // question and a uuid does not answer it. The id remains as
                // the fallback for an Agent this reader cannot list.
                render: (value: string) => named.get(value) ?? value,
              },
              { title: t("channelAppId"), dataIndex: "app_id", render: (v: string | null) => v ?? "—" },
              {
                title: t("channelKeyRef"),
                dataIndex: "encrypt_key_ref",
                // The name of the secret. Never its value — there is no
                // field on the response that could carry one.
                render: (v: string | null) => (v === null ? "—" : <Typography.Text code>{v}</Typography.Text>),
              },
              {
                // Whether this binding can answer at all. Without it a
                // receive-only binding and one wired to reply look
                // identical, and "the Agent answered but Feishu showed
                // nothing" has no visible cause on the page meant to
                // explain it. The reference itself is not shown — the
                // column is about capability, and the name is in the edit
                // dialog for whoever is changing it.
                title: t("channelReplies"),
                dataIndex: "app_secret_ref",
                render: (value: string | null) =>
                  value === null ? (
                    <Tag>{t("channelReceiveOnly")}</Tag>
                  ) : (
                    <Tag color="green">{t("channelCanReply")}</Tag>
                  ),
              },
              {
                // Until this route existed, the only way to see which
                // transport a binding used was to read the row in Postgres.
                // This shows the **stored** value, which between a switch
                // and the next scheduler restart is deliberately not the
                // one in use — migration 0052's own docstring is about that
                // gap.
                //
                // The note beside a long connection is unconditional, and
                // it says what it can actually know: that this transport
                // only comes into effect at a scheduler restart. It is
                // **not** a record of a restart still being owed — a
                // binding switched months ago and long since restarted
                // shows the same line. Nothing on this page could tell the
                // two apart: the response carries the stored transport and
                // nothing about the running scheduler, and the post-save
                // Alert above is a boolean in component state that a reload
                // clears. Whoever wants the note to mean "still owed" has
                // to give the API something to say it with.
                title: t("channelTransport"),
                dataIndex: "transport",
                render: (value: string | undefined) =>
                  value === "long_connection" ? (
                    <Space size="small">
                      <Tag>{t("channelTransportLongConnection")}</Tag>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {t("channelTransportRestartRequired")}
                      </Typography.Text>
                    </Space>
                  ) : value === "webhook" ? (
                    <Tag>{t("channelTransportWebhook")}</Tag>
                  ) : (
                    // Not folded into "Webhook". This column's whole job is
                    // to say what the stored value is, and a value this
                    // console has no wording for — a newer server, a
                    // hand-edited row — is exactly the one a reader has to
                    // be able to notice.
                    <Tag color="warning">{value ?? "—"}</Tag>
                  ),
              },
              { title: t("channelStatus"), dataIndex: "status", render: (v: string) => <Tag>{v}</Tag> },
              { title: t("channelBoundAt"), dataIndex: "created_at", render: (v: string) => moment(v) },
              {
                title: "",
                key: "actions",
                render: (_value, row) =>
                  row.status === "active" ? (
                    <Space size="small">
                      <Button
                        size="small"
                        onClick={() => {
                          setEditing(row);
                          // Spread rather than an explicit `undefined`:
                          // under `exactOptionalPropertyTypes` an optional
                          // field set to `undefined` is not the same as an
                          // absent one, and a receive-only binding has to
                          // leave the select genuinely unset.
                          editForm.setFieldsValue({
                            appId: row.app_id ?? "",
                            encryptKeyRef: row.encrypt_key_ref ?? "",
                            ...(row.app_secret_ref === null
                              ? {}
                              : { appSecretRef: row.app_secret_ref }),
                            transport: row.transport,
                          });
                        }}
                      >
                        {t("channelEdit")}
                      </Button>
                      <Button
                        danger
                        size="small"
                        loading={disable.isPending}
                        onClick={() => disable.mutate(row.id)}
                      >
                        {t("channelDisable")}
                      </Button>
                    </Space>
                  ) : null,
              },
            ]}
          />
        )}
      </Card>

      <Card
        title={t("channelIssuers")}
        variant="borderless"
        className="page-alert"
        loading={issuers.isPending}
        extra={
          <Button onClick={() => setRegistering(true)}>{t("registerIssuer")}</Button>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary">{t("channelIssuersIntro")}</Typography.Paragraph>
          {(issuers.data ?? []).length === 0 ? (
            <Empty description={t("channelIssuersEmpty")} />
          ) : (
            <Table<ChannelIssuerResponse>
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={issuers.data ?? []}
              columns={[
                { title: t("channelKind"), dataIndex: "channel", render: (v: string) => <Tag>{v}</Tag> },
                { title: t("issuerName"), dataIndex: "issuer" },
                {
                  title: t("issuerOrigins"),
                  dataIndex: "allowed_origins",
                  // Shown rather than hidden behind a detail view: an origin
                  // nobody remembers adding is how an embedded portal stops
                  // working, and this is the only list of them.
                  render: (value: string[]) => (value.length === 0 ? "—" : value.join(" ")),
                },
                {
                // Whether this binding can answer at all. Without it a
                // receive-only binding and one wired to reply look
                // identical, and "the Agent answered but Feishu showed
                // nothing" has no visible cause on the page meant to
                // explain it. The reference itself is not shown — the
                // column is about capability, and the name is in the edit
                // dialog for whoever is changing it.
                title: t("channelReplies"),
                dataIndex: "app_secret_ref",
                render: (value: string | null) =>
                  value === null ? (
                    <Tag>{t("channelReceiveOnly")}</Tag>
                  ) : (
                    <Tag color="green">{t("channelCanReply")}</Tag>
                  ),
              },
              { title: t("channelStatus"), dataIndex: "status", render: (v: string) => <Tag>{v}</Tag> },
                {
                  title: "",
                  key: "actions",
                  render: (_value, row) =>
                    row.status === "active" ? (
                      <Button
                        danger
                        size="small"
                        loading={disableIssuer.isPending}
                        onClick={() => disableIssuer.mutate(row.id)}
                      >
                        {t("channelDisable")}
                      </Button>
                    ) : null,
                },
              ]}
            />
          )}
        </Space>
      </Card>

      <Modal
        open={registering}
        title={t("registerIssuer")}
        okText={t("saveName")}
        cancelText={t("cancel")}
        confirmLoading={registerIssuer.isPending}
        onCancel={() => setRegistering(false)}
        onOk={() => void issuerForm.submit()}
      >
        <Form
          form={issuerForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => registerIssuer.mutate(values)}
        >
          <Typography.Paragraph type="secondary">{t("registerIssuerHint")}</Typography.Paragraph>
          <Form.Item name="issuer" label={t("issuerName")} rules={[{ required: true }]}>
            <Input placeholder="https://sso.example.com" />
          </Form.Item>
          <Form.Item name="keyMode" label={t("issuerKeyMode")} initialValue="jwks">
            <Radio.Group>
              <Radio value="jwks">{t("issuerKeyModeJwks")}</Radio>
              <Radio value="publicKey">{t("issuerKeyModePublicKey")}</Radio>
            </Radio.Group>
          </Form.Item>
          {issuerKeyMode === "publicKey" ? (
            <Form.Item name="publicKey" label={t("issuerPublicKey")} rules={[{ required: true }]}>
              <Input.TextArea rows={5} placeholder="-----BEGIN PUBLIC KEY-----" />
            </Form.Item>
          ) : (
            <Form.Item name="jwksUrl" label={t("issuerJwksUrl")} rules={[{ required: true }]}>
              <Input placeholder="https://sso.example.com/.well-known/jwks.json" />
            </Form.Item>
          )}
          <Form.Item name="origins" label={t("issuerOrigins")}>
            <Input.TextArea rows={3} placeholder="https://portal.example.com" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={open}
        title={t("bindChannel")}
        okText={t("bindChannelConfirm")}
        cancelText={t("cancel")}
        confirmLoading={bind.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => void form.submit()}
        okButtonProps={{ disabled: usable.length === 0 }}
      >
        {usable.length === 0 && !secrets.isPending ? (
          // Said before the form rather than after the refusal: a Feishu
          // binding cannot exist without a key reference (migration 0037's
          // CHECK), and there is nothing on this page that would fix it.
          <Alert type="info" showIcon message={t("channelsNeedSecret")} />
        ) : null}
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => bind.mutate(values)}
        >
          <Form.Item name="agentId" label={t("channelAgent")} rules={[{ required: true }]}>
            <Select
              options={(agents.data ?? []).map((agent) => ({ value: agent.id, label: agent.name }))}
            />
          </Form.Item>
          <Form.Item name="encryptKeyRef" label={t("channelKeyRef")} rules={[{ required: true }]}>
            {/* A choice among stored secrets, not a text field: a free-text
                reference is how you end up with a binding pointing at a
                secret that does not exist, which fails in a webhook hours
                later where nobody is watching. */}
            {/* Value is the id, label is the name. `CredentialResolver`
                resolves a Secret by id; a name is neither an id nor an
                environment variable, so a binding storing one validated
                cleanly and then failed at the first delivery. */}
            <Select options={usable.map((secret) => ({ value: secret.id, label: secret.name }))} />
          </Form.Item>
          <Form.Item name="appId" label={t("channelAppId")}>
            <Input />
          </Form.Item>
          <Form.Item
            name="appSecretRef"
            label={t("channelAppSecretRef")}
            extra={t("channelAppSecretRefHint")}
          >
            {/* Optional — a receive-only binding needs none. The same Select
                of stored secrets, so an unknown reference cannot be typed.
                No placeholder: on an antd Select a placeholder becomes the
                combobox's accessible name and hides the field's label. */}
            <Select
              allowClear
              options={usable.map((secret) => ({ value: secret.id, label: secret.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={editing !== null}
        title={t("channelEditTitle")}
        okText={t("channelEditConfirm")}
        confirmLoading={rewire.isPending}
        onCancel={() => {
          setEditing(null);
          editForm.resetFields();
        }}
        onOk={() => void editForm.submit()}
        destroyOnHidden
      >
        <Form form={editForm} layout="vertical" onFinish={(values) => rewire.mutate(values)}>
          {/* No Agent field. Moving a binding to another Agent would
              silently redirect every conversation already mapped to it,
              and the API refuses it for that reason — offering the control
              here would be a promise this platform does not keep. */}
          <Form.Item name="encryptKeyRef" label={t("channelKeyRef")} rules={[{ required: true }]}>
            <Select options={usable.map((secret) => ({ value: secret.id, label: secret.name }))} />
          </Form.Item>
          <Form.Item name="appId" label={t("channelAppId")}>
            <Input />
          </Form.Item>
          <Form.Item
            name="appSecretRef"
            label={t("channelAppSecretRef")}
            extra={t("channelAppSecretRefHint")}
          >
            <Select
              allowClear
              options={usable.map((secret) => ({ value: secret.id, label: secret.name }))}
            />
          </Form.Item>
          {/* The restart warning is not here as `extra`: it needs to survive
              this dialog closing (`onSuccess` closes it immediately), so it
              is a page-level Alert set from `rewire`'s `onSuccess` instead —
              see `transportRestartHint` above. Duplicating the text in both
              places would leave two matches for one string the moment this
              modal's close animation overlaps the Alert appearing. */}
          <Form.Item
            name="transport"
            label={t("channelTransport")}
            rules={[{ required: true }]}
            // Said before the choice, not after the refusal: the API's 400
            // says *that* it was refused, and this page is where the reason
            // — a missing app id or app secret reference — can be pointed at.
            extra={canHoldLongConnection ? null : t("channelTransportNeedsCredentials")}
          >
            <Select
              options={[
                { value: "webhook", label: t("channelTransportWebhook") },
                {
                  value: "long_connection",
                  label: t("channelTransportLongConnection"),
                  // The scheduler skips a `long_connection` binding with no
                  // app credentials (`api/cli.py`'s `continue`), leaving a
                  // binding that reads as switched on and receives nothing.
                  // The API refuses this too — it is public — and this only
                  // keeps somebody from having to be refused to find out.
                  disabled: !canHoldLongConnection,
                },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

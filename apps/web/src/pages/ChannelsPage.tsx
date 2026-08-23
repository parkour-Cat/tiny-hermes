import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Select, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type {
  AgentResponse,
  ChannelBindingResponse,
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
    enabled: workspaceId !== null && (open || nothingBound),
  });

  const bind = useMutation({
    mutationFn: (values: { agentId: string; encryptKeyRef: string; appId: string }) =>
      api<ChannelBindingResponse>("/api/v1/channel-bindings", {
        ...scope,
        method: "POST",
        body: JSON.stringify({
          channel: "feishu",
          agent_id: values.agentId,
          app_id: values.appId,
          encrypt_key_ref: values.encryptKeyRef,
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
              { title: t("channelStatus"), dataIndex: "status", render: (v: string) => <Tag>{v}</Tag> },
              { title: t("channelBoundAt"), dataIndex: "created_at", render: (v: string) => moment(v) },
              {
                title: "",
                key: "actions",
                render: (_value, row) =>
                  row.status === "active" ? (
                    <Button
                      danger
                      size="small"
                      loading={disable.isPending}
                      onClick={() => disable.mutate(row.id)}
                    >
                      {t("channelDisable")}
                    </Button>
                  ) : null,
              },
            ]}
          />
        )}
      </Card>

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
            <Select options={usable.map((secret) => ({ value: secret.name, label: secret.name }))} />
          </Form.Item>
          <Form.Item name="appId" label={t("channelAppId")}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

import { useMutation } from "@tanstack/react-query";
import { Alert, Button, Form, Input, Modal, Typography } from "antd";
import { useState } from "react";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { useMyRole } from "../workspace/useMyRole";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

export function SharedMemoryButton({ agentId, agentName }: { agentId: string; agentName: string }) {
  const t = useT();
  const { user } = useAuth();
  const workspaceId = useWorkspaceId();
  const { role } = useMyRole();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [form] = Form.useForm<{ body: string }>();
  const write = useMutation({
    mutationFn: ({ body }: { body: string }) => api("/api/v1/memories/shared", {
      workspace: workspaceId ?? "", method: "POST", body: JSON.stringify({ agent_id: agentId, body }),
    }),
    onSuccess: () => { form.resetFields(); setSaved(true); setOpen(false); },
    onError: (error) => form.setFields([{ name: "body", errors: [problemMessage(error, t)] }]),
  });
  if (!user?.is_platform_admin && role !== "workspace_admin" && role !== "platform_admin") return null;
  return <>
    <Button onClick={() => { setSaved(false); setOpen(true); }}>{t("writeShared")}</Button>
    {saved ? <Typography.Text type="success">{t("memorySaved")}</Typography.Text> : null}
    <Modal open={open} title={`${t("writeShared")} · ${agentName}`} okText={t("saveName")} cancelText={t("cancel")}
      confirmLoading={write.isPending} onCancel={() => setOpen(false)} onOk={() => void form.submit()}>
      <Alert className="page-alert" type="info" title={t("writeSharedHint")} />
      <Form form={form} layout="vertical" onFinish={(values) => write.mutate(values)}>
        <Form.Item name="body" label={t("memoryBody")} rules={[{ required: true, whitespace: true, message: t("required") }]}>
          <Input.TextArea rows={5} />
        </Form.Item>
      </Form>
    </Modal>
  </>;
}

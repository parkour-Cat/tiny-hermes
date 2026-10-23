import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, InputNumber, Space, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { LimitValues, WorkspaceLimitsResponse } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";

/** The four limits, in the order the page shows them (product design §1.21, v2.21). */
const FIELDS: { name: keyof LimitValues; label: MessageKey }[] = [
  { name: "agent_max_execution_seconds", label: "limitExecutionSeconds" },
  { name: "agent_max_model_calls", label: "limitModelCalls" },
  { name: "max_running_runs_per_caller", label: "limitRunningPerCaller" },
  { name: "max_running_runs", label: "limitRunning" },
];

/** Every member reads them: an Agent author has to know how far they may
 *  go. Only a workspace administrator saves; the API refuses anyone else. */
export function WorkspaceLimitsPage() {
  const t = useT();
  const { admin } = useWorkspacePermissions();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<LimitValues>();
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const scope = { workspace: workspaceId ?? "" };
  const key = ["workspace-limits", workspaceId] as const;

  const limits = useQuery({
    queryKey: key,
    queryFn: () => api<WorkspaceLimitsResponse>(`/api/v1/workspaces/${workspaceId}/limits`, scope),
    enabled: workspaceId !== null,
  });

  const save = useMutation({
    mutationFn: (values: LimitValues) =>
      api<WorkspaceLimitsResponse>(`/api/v1/workspaces/${workspaceId}/limits`, {
        ...scope,
        method: "PUT",
        // An emptied field is `null`, which puts that limit back to the
        // platform's default; `undefined` would leave it as it was.
        body: JSON.stringify(
          Object.fromEntries(FIELDS.map(({ name }) => [name, values[name] ?? null])),
        ),
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(key, updated);
      setError(null);
      setSaved(true);
    },
    onError: (caught) => {
      setSaved(false);
      setError(problemMessage(caught, t));
    },
  });

  const data = limits.data;
  return (
    <Card title={t("runLimits")}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Paragraph type="secondary">{t("runLimitsIntro")}</Typography.Paragraph>
        {!admin ? <Alert type="info" showIcon message={t("limitReadOnly")} /> : null}
        {error ? <Alert type="error" showIcon message={error} /> : null}
        {saved ? <Alert type="success" showIcon message={t("limitSaved")} /> : null}
        {data ? (
          <Form
            form={form}
            name="workspace-limits"
            layout="vertical"
            // Drawn only once the limits have arrived, so they are its
            // starting values rather than something filled in a render later.
            initialValues={data.values}
            disabled={!admin}
            onValuesChange={() => setSaved(false)}
            onFinish={(values) => save.mutate(values)}
          >
            {FIELDS.map(({ name, label }) => {
              const fallback = data.defaults[name];
              const ceiling = data.ceilings[name];
              return (
                <Form.Item
                  key={name}
                  name={name}
                  label={t(label)}
                  extra={ceiling === null ? null : t("limitCeiling").replace("{value}", String(ceiling))}
                >
                  <InputNumber
                    min={1}
                    {...(ceiling === null ? {} : { max: ceiling })}
                    precision={0}
                    style={{ width: 240 }}
                    placeholder={
                      fallback === null
                        ? t("limitNoDefault")
                        : t("limitDefault").replace("{value}", String(fallback))
                    }
                  />
                </Form.Item>
              );
            })}
            {admin ? (
              <Button type="primary" htmlType="submit" loading={save.isPending}>
                {t("limitSave")}
              </Button>
            ) : null}
          </Form>
        ) : null}
      </Space>
    </Card>
  );
}

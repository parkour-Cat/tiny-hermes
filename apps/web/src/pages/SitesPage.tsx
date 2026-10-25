import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { SiteResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { moment } from "../i18n/moment";
import { EmptyState } from "../ui/EmptyState";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";

/** 发布出去的网站（§1.26, v2.26）。链接只在发布时给一次，这里不列。 */
export function SitesPage() {
  const t = useT();
  const { admin, writer } = useWorkspacePermissions();
  const [modal, contextHolder] = Modal.useModal();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const key = ["sites", workspaceId] as const;

  const sites = useQuery({
    queryKey: key,
    queryFn: () => api<SiteResponse[]>("/api/v1/sites", scope),
    enabled: workspaceId !== null,
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  const failed = (caught: unknown) => setError(problemMessage(caught, t));

  const revoke = useMutation({
    mutationFn: (id: string) => api<void>(`/api/v1/sites/${id}/revoke`, { ...scope, method: "POST" }),
    onSuccess: refresh,
    onError: failed,
  });
  const extend = useMutation({
    mutationFn: (id: string) => api<{ expires_at: string }>(`/api/v1/sites/${id}/extend`, { ...scope, method: "POST" }),
    onSuccess: refresh,
    onError: failed,
  });
  const visibility = useMutation({
    mutationFn: ({ id, value }: { id: string; value: "members" | "link" }) =>
      api<{ visibility: string }>(`/api/v1/sites/${id}/visibility`, {
        ...scope,
        method: "PUT",
        body: JSON.stringify({ visibility: value }),
      }),
    onSuccess: refresh,
    onError: failed,
  });

  const status = (site: SiteResponse) =>
    site.revoked_at ? <Tag>{t("siteRevoked")}</Tag> : site.active ? <Tag color="green">{t("siteActive")}</Tag> : <Tag>{t("siteExpired")}</Tag>;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {contextHolder}
      <Typography.Paragraph type="secondary">{t("siteLinkNote")}</Typography.Paragraph>
      {error ? <Alert type="error" showIcon message={error} closable onClose={() => setError(null)} /> : null}
      {sites.isError ? (
        <Alert type="error" showIcon message={problemMessage(sites.error, t)} action={<Button onClick={() => void sites.refetch()}>{t("retry")}</Button>} />
      ) : sites.data && sites.data.length === 0 ? (
        <EmptyState title={t("sitesEmpty")} action={<Typography.Text type="secondary">{t("sitesEmptyHint")}</Typography.Text>} />
      ) : (
        (sites.data ?? []).map((site) => (
          <Card key={site.id} size="small" title={site.title ?? t("siteUntitled")} extra={status(site)}>
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Space wrap>
                <Typography.Text code>{site.root}</Typography.Text>
                <Tag>{site.visibility === "link" ? t("siteVisibilityLink") : t("siteVisibilityMembers")}</Tag>
                <Typography.Text type="secondary">{t("siteExpires").replace("{date}", moment(site.expires_at))}</Typography.Text>
              </Space>
              {site.active ? (
                <Space wrap>
                  {writer ? (
                    <Button size="small" loading={extend.isPending} onClick={() => extend.mutate(site.id)}>
                      {t("siteExtend")}
                    </Button>
                  ) : null}
                  {admin ? (
                    site.visibility === "members" ? (
                      <Button
                        size="small"
                        onClick={() =>
                          void modal.confirm({
                            title: t("siteOpenToAll"),
                            content: t("siteOpenConfirm"),
                            okText: t("siteOpenToAll"),
                            onOk: () => visibility.mutateAsync({ id: site.id, value: "link" }),
                          })
                        }
                      >
                        {t("siteOpenToAll")}
                      </Button>
                    ) : (
                      <Button size="small" onClick={() => visibility.mutate({ id: site.id, value: "members" })}>
                        {t("siteMembersOnly")}
                      </Button>
                    )
                  ) : null}
                  <Button
                    size="small"
                    danger
                    onClick={() =>
                      void modal.confirm({
                        title: t("siteRevoke"),
                        content: t("siteRevokeConfirm"),
                        okText: t("siteRevoke"),
                        okButtonProps: { danger: true },
                        onOk: () => revoke.mutateAsync(site.id),
                      })
                    }
                  >
                    {t("siteRevoke")}
                  </Button>
                </Space>
              ) : null}
            </Space>
          </Card>
        ))
      )}
    </Space>
  );
}

import { useQueries, useQuery } from "@tanstack/react-query";
import { Alert, Avatar, Button, Card, Typography } from "antd";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { ChatChrome } from "../layout/ChatChrome";
import { PageHeading } from "../layout/ConsoleChrome";
import { EmptyState } from "../ui/EmptyState";

type Workspace = {
  id: string;
  name: string;
  status: string;
};

type Listed = {
  workspace: Workspace;
  agent: AgentResponse;
};

export function ChatHomePage() {
  const t = useT();
  const workspaces = useQuery({
    queryKey: ["workspaces"] as const,
    queryFn: () => api<Workspace[]>("/api/v1/workspaces"),
  });
  const listings = useQueries({
    queries: (workspaces.data ?? []).map((workspace) => ({
      queryKey: ["agents", workspace.id] as const,
      queryFn: () => api<AgentResponse[]>("/api/v1/agents", { workspace: workspace.id }),
    })),
  });

  const failed = [workspaces, ...listings].find((query) => query.isError);
  const pending = workspaces.isPending || listings.some((query) => query.isPending);
  const rows: Listed[] = (workspaces.data ?? []).flatMap((workspace, index) =>
    (listings[index]?.data ?? [])
      .filter((agent) => agent.current_version_id !== null)
      .map((agent) => ({ workspace, agent })),
  );

  return (
    <ChatChrome>
      <PageHeading kicker={t("chatKicker")} title={t("chatTitle")} intro={t("chatIntro")} />
      {failed === undefined ? null : (
        <Alert
          type="error"
          title={problemMessage(failed.error)}
          action={<Button onClick={() => void workspaces.refetch()}>{t("retry")}</Button>}
          showIcon
        />
      )}
      {pending ? (
        <Card loading variant="borderless" />
      ) : rows.length === 0 ? (
        <Card variant="borderless">
          <EmptyState title={t("emptyPublishedAgents")} />
        </Card>
      ) : (
        <Card variant="borderless">
          <div className="workspace-list" role="list">
            {rows.map(({ workspace, agent }) => (
              <article
                className="workspace-row"
                role="listitem"
                aria-label={agent.name}
                key={`${workspace.id}:${agent.id}`}
              >
                <Avatar shape="square">{agent.name.slice(0, 1)}</Avatar>
                <div className="workspace-summary">
                  <Typography.Title level={4}>
                    <Link to={`/chat/${workspace.id}/agents/${agent.id}`}>{agent.name}</Link>
                  </Typography.Title>
                  <Typography.Text type="secondary">{workspace.name}</Typography.Text>
                </div>
              </article>
            ))}
          </div>
        </Card>
      )}
    </ChatChrome>
  );
}

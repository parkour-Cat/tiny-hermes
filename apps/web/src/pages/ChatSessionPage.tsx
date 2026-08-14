import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api } from "../api/client";
import type { AgentResponse } from "../api/types";
import { ChatChrome } from "../layout/ChatChrome";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { PlaygroundPage } from "./PlaygroundPage";

export function ChatSessionPage() {
  const workspaceId = useWorkspaceId();
  const { agentId = "" } = useParams();
  const agent = useQuery({
    queryKey: ["agent", workspaceId, agentId] as const,
    queryFn: () => api<AgentResponse>(`/api/v1/agents/${agentId}`, { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null && agentId !== "",
  });

  return (
    <ChatChrome title={agent.data?.name}>
      <PlaygroundPage />
    </ChatChrome>
  );
}

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Modal, Select, Space, Spin, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, AgentSpecDocument, SkillFilePayload } from "../api/types";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

type Kind = "skills" | "http_tools" | "mcp_tools";
function bound(spec: AgentSpecDocument, kind: Kind, versionId: string): boolean {
  if (kind === "skills") return (spec.skills ?? []).some((entry) => entry.skill_version_id === versionId);
  if (kind === "http_tools") return (spec.http_tools ?? []).some((entry) => entry.http_tool_version_id === versionId);
  return (spec.mcp_tools ?? []).some((entry) => entry.mcp_server_version_id === versionId);
}

export function CapabilityUsage({ kind, versionId }: { kind: Kind; versionId: string }) {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const [open, setOpen] = useState(false);
  const scope = { workspace: workspaceId ?? "" };
  const usage = useQuery({ queryKey: ["capability-usage", workspaceId, kind, versionId], enabled: open && workspaceId !== null,
    queryFn: async () => {
      const agents = await api<AgentResponse[]>("/api/v1/agents", scope);
      return Promise.all(agents.map(async (agent) => {
        const [draft, published] = await Promise.all([
          api<{ spec: AgentSpecDocument }>(`/api/v1/agents/${agent.id}/draft`, scope),
          agent.current_version_id ? api<{ spec: AgentSpecDocument }>(`/api/v1/agents/${agent.id}/versions/${agent.current_version_id}`, scope) : null,
        ]);
        return { agent, draft: bound(draft.spec, kind, versionId), published: published ? bound(published.spec, kind, versionId) : false };
      }));
    },
  });
  const matches = (usage.data ?? []).filter((entry) => entry.draft || entry.published);
  return <><Button size="small" onClick={() => setOpen(true)}>{t("capabilityUsage")}</Button>
    <Modal open={open} title={t("capabilityUsage")} footer={null} onCancel={() => setOpen(false)}>
      <Typography.Paragraph>{t("capabilityVersionHint")}</Typography.Paragraph>
      {usage.isPending ? <Spin /> : usage.isError ? <Alert type="error" title={problemMessage(usage.error, t)} action={<Button onClick={() => void usage.refetch()}>{t("retry")}</Button>} /> : matches.length === 0 ? <Typography.Paragraph>{t("capabilityUnused")}</Typography.Paragraph> : matches.map(({ agent, draft, published }) =>
        <div className="workspace-row" key={agent.id}><Link to={`/workspaces/${workspaceId}/agents/${agent.id}`}>{agent.name}</Link><Space wrap>{published && <Tag>{t("capabilityPublishedUse")}</Tag>}{draft && <Tag>{t("capabilityDraftUse")}</Tag>}</Space></div>)}
      <Link to={`/workspaces/${workspaceId}/agents`}>{t("capabilityBind")}</Link>
    </Modal></>;
}

export function SkillPreview({ skillId, versionId }: { skillId: string; versionId: string }) {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("SKILL.md");
  const detail = useQuery({ queryKey: ["skill-preview", workspaceId, skillId, versionId], enabled: open && workspaceId !== null,
    queryFn: () => api<{ files: SkillFilePayload[] }>(`/api/v1/skills/${skillId}/versions/${versionId}`, { workspace: workspaceId ?? "" }),
  });
  return <><Button size="small" onClick={() => setOpen(true)}>{t("capabilityPreview")}</Button><Modal open={open} width={780} title={t("capabilityPreview")} footer={null} onCancel={() => setOpen(false)}>
    {detail.isPending ? <Spin /> : detail.isError ? <Alert type="error" title={problemMessage(detail.error, t)} action={<Button onClick={() => void detail.refetch()}>{t("retry")}</Button>} /> : <>
      <Select aria-label={t("capabilityPreview")} value={path} onChange={setPath} style={{ width: "100%" }} options={(detail.data.files ?? []).map((file) => ({ value: file.path, label: file.path }))} />
      <pre className="skill-file-body" style={{ maxHeight: "60vh", overflow: "auto" }}>{detail.data.files.find((file) => file.path === path)?.content ?? ""}</pre>
    </>}
  </Modal></>;
}

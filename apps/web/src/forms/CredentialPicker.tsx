import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Select, Input, Button } from "antd";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { SecretResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

export function CredentialPicker({ purpose, value, onChange, id }: {
  purpose: string; value?: string; onChange?: (value: string) => void; id?: string;
}) {
  const workspaceId = useWorkspaceId();
  const t = useT();
  const [manual, setManual] = useState(false);
  const secrets = useQuery({ queryKey: ["secrets", workspaceId],
    queryFn: () => api<SecretResponse[]>("/api/v1/secrets", { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null,
  });
  return <div>
    {manual ? <Input id={id} value={value} onChange={(event) => onChange?.(event.target.value)} /> : <Select {...(id ? { id } : {})} value={value || undefined} onChange={(next) => onChange?.(next ?? "")} allowClear showSearch style={{ width: "100%" }}
      placeholder={t("credentialChoose")}
      options={(secrets.data ?? []).filter((secret) => secret.status === "active" && (!secret.purpose || secret.purpose === "general" || secret.purpose === purpose))
        .filter((secret) => purpose !== "login" || secret.scope === "platform")
        .map((secret) => ({ value: secret.id, label: `${secret.name} · ${t(secret.scope === "platform" ? "secretScopePlatform" : "secretScopeWorkspace")}` }))}
      filterOption={(input, option) => `${option?.label ?? ""} ${option?.value ?? ""}`.toLowerCase().includes(input.toLowerCase())} />}
    <Button type="link" size="small" onClick={() => setManual(!manual)}>{t(manual ? "credentialChoose" : "credentialManual")}</Button>
    {secrets.isError && <Alert type="warning" title={problemMessage(secrets.error, t)} action={<Button onClick={() => void secrets.refetch()}>{t("retry")}</Button>} />}
    <small>{t("credentialReferenceHint")} <Link target="_blank" rel="noopener noreferrer" to={`/workspaces/${workspaceId}/${purpose === "login" ? "platform" : "settings"}#secrets`}>{t("newSecret")}</Link></small>
  </div>;
}

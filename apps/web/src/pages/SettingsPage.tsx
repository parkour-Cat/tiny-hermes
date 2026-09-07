import { GroupedPage } from "../layout/GroupedPage";
import { ApiKeysPage } from "./ApiKeysPage";
import { Link, Navigate, useLocation } from "react-router-dom";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";
import { useT } from "../i18n/locale";
import { MembersPage } from "./MembersPage";
import { ModelEndpointsPage } from "./ModelEndpointsPage";
import { OutboundScopePage } from "./OutboundScopePage";
import { SecretsPage } from "./SecretsPage";

/** 配一次就不太会再动的东西。六段，按角色决定画哪几段——见 GroupedPage。 */
export function SettingsPage() {
  const { platform } = useWorkspacePermissions();
  const location = useLocation();
  const t = useT();
  if (location.hash === "#identity-providers") return <Navigate to="../platform#identity-providers" replace />;
  return (
    <GroupedPage
      groupKey="settings"
      render={(key) =>
        key === "members" ? <MembersPage /> :
        key === "api-keys" ? <ApiKeysPage /> :
        key === "model-endpoints" ? <>{platform ? <Link to="../platform#model-endpoints">{t("managePlatform")}</Link> : null}<ModelEndpointsPage readOnly /></> :
        key === "secrets" ? <SecretsPage scopeOnly="workspace" /> :
        key === "outbound" ? <OutboundScopePage mode="workspace" /> : null
      }
    />
  );
}

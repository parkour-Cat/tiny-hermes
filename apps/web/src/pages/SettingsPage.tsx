import { GroupedPage } from "../layout/GroupedPage";
import { ApiKeysPage } from "./ApiKeysPage";
import { IdentityProvidersPage } from "./IdentityProvidersPage";
import { MembersPage } from "./MembersPage";
import { ModelEndpointsPage } from "./ModelEndpointsPage";
import { OutboundScopePage } from "./OutboundScopePage";
import { SecretsPage } from "./SecretsPage";

/** 配一次就不太会再动的东西。六段，按角色决定画哪几段——见 GroupedPage。 */
export function SettingsPage() {
  return (
    <GroupedPage
      groupKey="settings"
      render={(key) =>
        key === "members" ? <MembersPage /> :
        key === "api-keys" ? <ApiKeysPage /> :
        key === "identity-providers" ? <IdentityProvidersPage /> :
        key === "model-endpoints" ? <ModelEndpointsPage /> :
        key === "secrets" ? <SecretsPage /> :
        key === "outbound" ? <OutboundScopePage /> : null
      }
    />
  );
}

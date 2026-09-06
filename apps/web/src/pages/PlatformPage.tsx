import { GroupedPage } from "../layout/GroupedPage";
import { IdentityProvidersPage } from "./IdentityProvidersPage";
import { ModelEndpointsPage } from "./ModelEndpointsPage";
import { OutboundScopePage } from "./OutboundScopePage";
import { SecretsPage } from "./SecretsPage";

export function PlatformPage() {
  return <GroupedPage groupKey="platform" render={(key) =>
    key === "model-endpoints" ? <ModelEndpointsPage /> :
    key === "secrets" ? <SecretsPage scopeOnly="platform" /> :
    key === "outbound" ? <OutboundScopePage mode="platform" /> :
    key === "identity-providers" ? <IdentityProvidersPage /> : null
  } />;
}

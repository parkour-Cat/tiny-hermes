import { Alert, Button, Spin } from "antd";
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { QueryProvider } from "./api/QueryProvider";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { LocaleProvider, useT } from "./i18n/locale";
import { ConsoleTheme } from "./layout/ConsoleTheme";

const ConsoleLayout = lazy(() =>
  import("./layout/ConsoleLayout").then((module) => ({ default: module.ConsoleLayout })),
);
const AgentsPage = lazy(() =>
  import("./pages/AgentsPage").then((module) => ({ default: module.AgentsPage })),
);
const AgentDetailPage = lazy(() =>
  import("./pages/AgentDetailPage").then((module) => ({ default: module.AgentDetailPage })),
);
const BootstrapPage = lazy(() =>
  import("./pages/BootstrapPage").then((module) => ({ default: module.BootstrapPage })),
);
const LoginPage = lazy(() =>
  import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })),
);
const RunDetailPage = lazy(() =>
  import("./pages/RunDetailPage").then((module) => ({ default: module.RunDetailPage })),
);
const RunsPage = lazy(() =>
  import("./pages/RunsPage").then((module) => ({ default: module.RunsPage })),
);
const WorkspacesPage = lazy(() =>
  import("./pages/WorkspacesPage").then((module) => ({ default: module.WorkspacesPage })),
);
const PlaygroundPage = lazy(() =>
  import("./pages/PlaygroundPage").then((module) => ({ default: module.PlaygroundPage })),
);
const MembersPage = lazy(() =>
  import("./pages/MembersPage").then((module) => ({ default: module.MembersPage })),
);
const ApiKeysPage = lazy(() =>
  import("./pages/ApiKeysPage").then((module) => ({ default: module.ApiKeysPage })),
);
const ModelEndpointsPage = lazy(() =>
  import("./pages/ModelEndpointsPage").then((module) => ({ default: module.ModelEndpointsPage })),
);
const SecretsPage = lazy(() =>
  import("./pages/SecretsPage").then((module) => ({ default: module.SecretsPage })),
);
const AuditPage = lazy(() =>
  import("./pages/AuditPage").then((module) => ({
    default: module.AuditPage,
  })),
);
const ApprovalsPage = lazy(() =>
  import("./pages/ApprovalsPage").then((module) => ({
    default: module.ApprovalsPage,
  })),
);
const HttpToolsPage = lazy(() =>
  import("./pages/HttpToolsPage").then((module) => ({
    default: module.HttpToolsPage,
  })),
);
const McpServersPage = lazy(() =>
  import("./pages/McpServersPage").then((module) => ({
    default: module.McpServersPage,
  })),
);
const MemoryPage = lazy(() =>
  import("./pages/MemoryPage").then((module) => ({
    default: module.MemoryPage,
  })),
);
const OutboundScopePage = lazy(() =>
  import("./pages/OutboundScopePage").then((module) => ({
    default: module.OutboundScopePage,
  })),
);
const SkillsPage = lazy(() =>
  import("./pages/SkillsPage").then((module) => ({ default: module.SkillsPage })),
);
const SkillProposalsPage = lazy(() =>
  import("./pages/SkillProposalsPage").then((module) => ({
    default: module.SkillProposalsPage,
  })),
);
const UsagePage = lazy(() =>
  import("./pages/UsagePage").then((module) => ({ default: module.UsagePage })),
);

function AppRoutes() {
  const t = useT();
  const auth = useAuth();
  if (auth.loading) {
    return <Spin fullscreen description={t("loading")} />;
  }
  if (auth.error !== null) {
    return (
      <main className="centered-state">
        <Alert
          type="error"
          title={auth.error}
          action={<Button onClick={() => void auth.refresh()}>{t("retry")}</Button>}
          showIcon
        />
      </main>
    );
  }

  return (
    <Suspense fallback={<Spin fullscreen description={t("loading")} />}>
      <Routes>
        <Route path="/bootstrap" element={<BootstrapPage />} />
        <Route
          path="/login"
          element={auth.user === null ? <LoginPage /> : <Navigate to="/workspaces" replace />}
        />
        <Route
          path="/workspaces"
          element={auth.user === null ? <Navigate to="/login" replace /> : <WorkspacesPage />}
        />
        {/* Scope is a route parameter, so a reload or a shared link reopens the
            same Workspace and reaching for another one is addressable. */}
        <Route
          path="/workspaces/:workspaceId"
          element={auth.user === null ? <Navigate to="/login" replace /> : <ConsoleLayout />}
        >
          <Route index element={<Navigate to="agents" replace />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="agents/:agentId" element={<AgentDetailPage />} />
          <Route path="agents/:agentId/playground" element={<PlaygroundPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunDetailPage />} />
          <Route path="usage" element={<UsagePage />} />
          <Route path="members" element={<MembersPage />} />
          <Route path="api-keys" element={<ApiKeysPage />} />
          <Route path="model-endpoints" element={<ModelEndpointsPage />} />
          <Route path="secrets" element={<SecretsPage />} />
          <Route path="outbound" element={<OutboundScopePage />} />
          <Route path="skills" element={<SkillsPage />} />
          <Route path="skill-proposals" element={<SkillProposalsPage />} />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="http-tools" element={<HttpToolsPage />} />
          <Route path="mcp-servers" element={<McpServersPage />} />
          <Route path="memory" element={<MemoryPage />} />
        </Route>
        <Route
          path="*"
          element={<Navigate to={auth.user === null ? "/login" : "/workspaces"} replace />}
        />
      </Routes>
    </Suspense>
  );
}

export function App() {
  return (
    <ConsoleTheme>
      <LocaleProvider>
        <BrowserRouter>
          <AuthProvider>
            <QueryProvider>
              <AppRoutes />
            </QueryProvider>
          </AuthProvider>
        </BrowserRouter>
      </LocaleProvider>
    </ConsoleTheme>
  );
}

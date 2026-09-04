import { Alert, Button, Spin } from "antd";
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { QueryProvider } from "./api/QueryProvider";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { LocaleProvider, useT } from "./i18n/locale";
import { ConsoleTheme } from "./layout/ConsoleTheme";
import { LEGACY_REDIRECTS } from "./layout/redirects";

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
const InboxPage = lazy(() =>
  import("./pages/InboxPage").then((module) => ({ default: module.InboxPage })),
);
const ToolingPage = lazy(() =>
  import("./pages/ToolingPage").then((module) => ({ default: module.ToolingPage })),
);
const RecordsPage = lazy(() =>
  import("./pages/RecordsPage").then((module) => ({ default: module.RecordsPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);
const ChannelsPage = lazy(() =>
  import("./pages/ChannelsPage").then((module) => ({
    default: module.ChannelsPage,
  })),
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
          <Route path="channels" element={<ChannelsPage />} />
          <Route path="inbox" element={<InboxPage />} />
          <Route path="tooling" element={<ToolingPage />} />
          <Route path="records" element={<RecordsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          {/* 旧地址。**长期保留**：一个能打开的链接不会因为新导航上线就变得
              不该打开。锚点让它落在对应的段上，而不只是那一页的顶部。 */}
          {LEGACY_REDIRECTS.map(([from, to, anchor]) => (
            <Route
              key={from}
              path={from}
              element={<Navigate to={`../${to}#${anchor}`} replace />}
            />
          ))}
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

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Alert, Button, ConfigProvider, Spin } from "antd";
import { lazy, Suspense, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { t } from "./i18n/zh-CN";

const BootstrapPage = lazy(() =>
  import("./pages/BootstrapPage").then((module) => ({ default: module.BootstrapPage })),
);
const LoginPage = lazy(() =>
  import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })),
);
const WorkspacesPage = lazy(() =>
  import("./pages/WorkspacesPage").then((module) => ({ default: module.WorkspacesPage })),
);

function AppRoutes() {
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
        <Route
          path="*"
          element={<Navigate to={auth.user === null ? "/login" : "/workspaces"} replace />}
        />
      </Routes>
    </Suspense>
  );
}

export function App() {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false } },
      }),
  );
  return (
    <ConfigProvider
      button={{ autoInsertSpace: false }}
      theme={{
        token: {
          colorPrimary: "#155e75",
          borderRadius: 10,
          fontFamily: 'Inter, "Noto Sans SC", system-ui, sans-serif',
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <AppRoutes />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  );
}

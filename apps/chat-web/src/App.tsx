import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { QueryProvider } from "./api/QueryProvider";
import { useAuth, AuthProvider } from "./auth/AuthProvider";
import { LocaleProvider, useT } from "./i18n/locale";
import { ChatHome } from "./pages/ChatHome";
import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ChatTheme } from "./theme/ChatTheme";

function AppRoutes() {
  const t = useT();
  const auth = useAuth();
  if (auth.loading) {
    return <p className="centered">{t("loading")}</p>;
  }
  if (auth.error !== null) {
    return (
      <p className="centered">
        {auth.error}
        <button type="button" onClick={() => void auth.refresh()}>
          {t("retry")}
        </button>
      </p>
    );
  }

  return (
    <Routes>
      <Route
        path="/login"
        element={auth.user === null ? <LoginPage /> : <Navigate to="/" replace />}
      />
      <Route
        path="/"
        element={auth.user === null ? <Navigate to="/login" replace /> : <ChatHome />}
      />
      <Route
        path="/settings"
        element={auth.user === null ? <Navigate to="/login" replace /> : <SettingsPage />}
      />
      <Route
        path="/:left/:middle/:right"
        element={auth.user === null ? <Navigate to="/login" replace /> : <ChatPage />}
      />
      <Route
        path="/:left/:middle"
        element={auth.user === null ? <Navigate to="/login" replace /> : <ChatPage />}
      />
      <Route
        path="/:left"
        element={auth.user === null ? <Navigate to="/login" replace /> : <ChatPage />}
      />
      <Route path="*" element={<Navigate to={auth.user === null ? "/login" : "/"} replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <ChatTheme>
      <LocaleProvider>
        <BrowserRouter>
          <AuthProvider>
            <QueryProvider>
              <AppRoutes />
            </QueryProvider>
          </AuthProvider>
        </BrowserRouter>
      </LocaleProvider>
    </ChatTheme>
  );
}

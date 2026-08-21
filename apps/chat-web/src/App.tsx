import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { QueryProvider } from "./api/QueryProvider";
import { useAuth, AuthProvider } from "./auth/AuthProvider";
import { LocaleProvider, useT } from "./i18n/locale";
import { ChatPage } from "./pages/ChatPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ChatTheme } from "./theme/ChatTheme";

/**
 * No `/login` route. Design §4.5.1's red line — the platform is not an
 * identity provider — means there is nothing here for a person to type: an
 * end user proves who they are to the enterprise, never to this app, so
 * `loading`/`error`/`lost` are the only three things this surface can ever
 * show before a conversation, and none of them is a form.
 */
function AppRoutes() {
  const t = useT();
  const auth = useAuth();

  if (auth.loading) {
    return <p className="centered">{t("loading")}</p>;
  }
  if (auth.error !== null) {
    return (
      <main className="auth">
        <h1>{t("connectFailedTitle")}</h1>
        <p className="auth-intro">{t("connectFailedHint")}</p>
        <p className="auth-error">{auth.error}</p>
      </main>
    );
  }
  if (auth.lost) {
    return (
      <main className="auth">
        <h1>{t("connectLostTitle")}</h1>
        <p className="auth-intro">{t("connectLostHint")}</p>
      </main>
    );
  }

  return (
    <Routes>
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/:alias/:sessionRef" element={<ChatPage />} />
      <Route path="/:alias" element={<ChatPage />} />
      <Route
        path="/"
        element={
          <main className="auth">
            <h1>{t("connectWaitingTitle")}</h1>
            <p className="auth-intro">{t("connectWaitingHint")}</p>
          </main>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
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

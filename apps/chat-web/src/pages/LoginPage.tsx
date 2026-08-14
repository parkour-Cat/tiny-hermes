import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { HermesMark } from "../ui/HermesMark";

export function LoginPage() {
  const t = useT();
  const auth = useAuth();
  const navigate = useNavigate();
  const [subject, setSubject] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(): Promise<void> {
    if (subject.trim() === "" || password === "") {
      setError(t("required"));
      return;
    }
    if (!subject.includes("@")) {
      setError(t("invalidEmail"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await auth.login({ subject: subject.trim(), password });
      navigate("/", { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("loginFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth">
      <HermesMark variant="hero" size={220} />
      <h1>{t("loginTitle")}</h1>
      <p className="auth-intro">{t("loginIntro")}</p>
      {error === null ? null : <p className="auth-error">{error}</p>}
      <form
        className="auth-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label>
          {t("email")}
          <input
            type="email"
            autoComplete="email"
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
            required
          />
        </label>
        <label>
          {t("password")}
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={submitting}>
          {t("login")}
        </button>
      </form>
    </main>
  );
}

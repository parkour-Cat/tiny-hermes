import { t } from "../i18n/zh-CN";

type ProblemDetails = {
  code?: string;
  detail?: string;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function csrfToken(): string | undefined {
  const value = document.cookie
    .split("; ")
    .find((part) => part.startsWith("tiny_hermes_csrf="))
    ?.split("=")[1];
  return value === undefined ? undefined : decodeURIComponent(value);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const method = (init.method ?? "GET").toUpperCase();
  const csrf = csrfToken();
  if (["POST", "PATCH", "DELETE"].includes(method) && csrf !== undefined) {
    headers.set("X-CSRF-Token", csrf);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers,
    });
  } catch {
    throw new ApiError(0, "network_failed", t("networkFailed"));
  }

  if (!response.ok) {
    let problem: ProblemDetails;
    try {
      problem = (await response.json()) as ProblemDetails;
    } catch {
      problem = {};
    }
    throw new ApiError(
      response.status,
      problem.code ?? "request_failed",
      problem.detail ?? t("requestFailed"),
    );
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

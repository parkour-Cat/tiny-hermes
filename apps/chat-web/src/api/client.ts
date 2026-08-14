import { t } from "../i18n/zh-CN";

type ProblemDetails = {
  code?: string;
  detail?: string;
  context?: Record<string, unknown>;
};

export type ApiInit = RequestInit & {
  /**
   * The Workspace this request is scoped to, sent as `X-Workspace-Id`.
   *
   * Passed explicitly: an ambient workspace makes a wrong-scope call look
   * exactly like a right one, and chat-web must not guess the tenant.
   */
  workspace?: string;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly context: Record<string, unknown> = {},
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

export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { workspace, ...request } = init;
  const headers = new Headers(request.headers);
  headers.set("Content-Type", "application/json");
  if (workspace !== undefined) {
    headers.set("X-Workspace-Id", workspace);
  }

  const method = (request.method ?? "GET").toUpperCase();
  const csrf = csrfToken();
  if (method !== "GET" && method !== "HEAD" && csrf !== undefined) {
    headers.set("X-CSRF-Token", csrf);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...request,
      credentials: "include",
      headers,
    });
  } catch {
    throw new ApiError(0, "network_failed", t("networkFailed"));
  }

  if (!response.ok) {
    throw await asApiError(response);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export async function asApiError(response: Response): Promise<ApiError> {
  let problem: ProblemDetails;
  try {
    problem = (await response.json()) as ProblemDetails;
  } catch {
    problem = {};
  }
  return new ApiError(
    response.status,
    problem.code ?? "request_failed",
    problem.detail ?? t("requestFailed"),
    problem.context ?? {},
  );
}

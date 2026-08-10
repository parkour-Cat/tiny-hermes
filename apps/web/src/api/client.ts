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
   * Passed explicitly rather than read from a context or a module-level store:
   * an ambient workspace makes a wrong-scope call look exactly like a right one
   * at the call site, and scope is the one thing this console must never get
   * quietly wrong. The route parameter is the single source (`useWorkspaceId`).
   */
  workspace?: string;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    /**
     * Problem Details `context`. The event stream's `410` puts
     * `earliest_available_sequence` here, and without it the console cannot
     * resynchronize honestly.
     */
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

export type ApiResult<T> = {
  data: T;
  /**
   * The response status.
   *
   * Some routes say something with it that the body does not: publishing
   * answers `201` for a new version and `200` for content that was already
   * published, and those are different things to tell the user.
   */
  status: number;
};

export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  return (await apiWithStatus<T>(path, init)).data;
}

export async function apiWithStatus<T>(path: string, init: ApiInit = {}): Promise<ApiResult<T>> {
  const { workspace, ...request } = init;
  const headers = new Headers(request.headers);
  headers.set("Content-Type", "application/json");
  if (workspace !== undefined) {
    headers.set("X-Workspace-Id", workspace);
  }

  const method = (request.method ?? "GET").toUpperCase();
  const csrf = csrfToken();
  // Stated by exclusion, not as an allowlist of write methods. The allowlist
  // this replaces omitted PUT, so saving an agent draft would have failed with
  // csrf_failed; the backend gates CSRF on the write dependency rather than on
  // a list of methods, and this now says the same thing.
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
  return {
    data: response.status === 204 ? (undefined as T) : ((await response.json()) as T),
    status: response.status,
  };
}

/** Reads a Problem Details body into an `ApiError`, tolerating a missing one. */
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

import type {
  AgentResponse,
  ArtifactResponse,
  CanonicalMessage,
  RunResponse,
  SessionResponse,
} from "../api/types";

/**
 * Whoever is holding this conversation, as the chat surface needs them.
 *
 * Deliberately three fields. The console's own user carries `status` and
 * `is_platform_admin`, which belong to a control console and mean nothing to
 * a person talking to an Agent; a chat page that reads them is a chat page
 * that cannot be shown to an end user later.
 */
export type Viewer = {
  id: string;
  displayName: string;
  /**
   * How this identity is addressed — an email for a platform member, whatever
   * an end-user identity turns out to use. Shown in the account menu, so it
   * cannot be dropped; kept generic, so it does not have to be an email.
   */
  subject: string;
};

export type SignInInput = {
  subject: string;
  password: string;
};

/**
 * Everything the chat surface asks of a backend, named by what it wants
 * rather than by the route that answers today.
 *
 * The point of this interface is what it does *not* mention: no workspace, no
 * CSRF cookie, no `/api/v1`. Product design §4.5 makes an end user a separate
 * identity from a workspace member, and §7.1 makes their Web Chat a separate
 * entry point — so the surface that serves them will not be scoped by a
 * workspace at all. Keeping that vocabulary out of the components is what
 * makes the second implementation a sibling file instead of a rewrite.
 */
export interface ChatBackend {
  /**
   * An opaque, stable string identifying what this backend is scoped to.
   *
   * React Query keys need to change when the scope does, and the components
   * must not learn what a scope is made of to build one.
   */
  readonly scopeKey: string;

  /** The signed-in viewer, or `null` when nobody is. */
  viewer(): Promise<Viewer | null>;
  signIn(input: SignInInput): Promise<Viewer>;
  signOut(): Promise<void>;

  agent(agentId: string): Promise<AgentResponse>;
  listSessions(): Promise<SessionResponse[]>;
  createSession(agentId: string): Promise<SessionResponse>;
  messages(sessionId: string): Promise<CanonicalMessage[]>;

  startRun(sessionId: string, input: string): Promise<RunResponse>;
  run(runId: string): Promise<RunResponse>;
  act(runId: string, action: string, expectedStateVersion: number): Promise<RunResponse>;

  artifacts(runId: string): Promise<ArtifactResponse[]>;
  downloadArtifact(artifactId: string, filename: string): Promise<void>;

  /**
   * One connection to a Run's event stream, already authenticated.
   *
   * A `Response` rather than an `EventSource`: the stream route answers `410`
   * with the earliest sequence it still has, and `EventSource` closes on a
   * non-200 without exposing status or body, which would turn a truncated
   * timeline into one that reads as complete.
   */
  openEventStream(runId: string, cursor: number, signal: AbortSignal): Promise<Response>;
}

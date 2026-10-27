import { useQuery } from "@tanstack/react-query";
import { Alert, Button } from "antd";
import { useId, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { AgentResponse, RunResponse } from "../api/types";
import { useLocale } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useInboxCount } from "../layout/useInboxCount";
import { fill } from "../runs/explain";
import { PageHeading } from "../ui/PageHeading";
import { StatusTag } from "../ui/StatusTag";
import { useWorkspaceId } from "../workspace/useWorkspaceId";
import { useWorkspacePermissions } from "../workspace/WorkspacePermissions";

const DAY = 24 * 60 * 60 * 1000;

function seconds(run: RunResponse): number | null {
  if (run.started_at === null || run.finished_at === null) return null;
  return Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000));
}

/**
 * The first page after choosing a workspace: what needs a person, then how
 * today went, then where to go next.
 *
 * Every number here is computed from a complete listing — the run list is
 * asked without a `limit`, filtered by time and status on the server — so a
 * figure that looks exact is exact. A page of results counted here would be
 * a lower bound dressed as a total.
 */
export function OverviewPage() {
  const { t, locale } = useLocale();
  const workspaceId = useWorkspaceId() ?? "";
  const { writer } = useWorkspacePermissions();
  const base = `/workspaces/${workspaceId}`;
  const scope = { workspace: workspaceId };

  const navigate = useNavigate();
  // Fixed for the life of the page, so the query keys do not change on every render.
  const [midnight] = useState(() => {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    return start.toISOString();
  });
  const [weekAgo] = useState(() => new Date(Date.now() - 7 * DAY).toISOString());

  const today = useQuery({
    queryKey: ["home-runs-today", workspaceId, midnight],
    queryFn: () => api<RunResponse[]>(`/api/v1/runs?since=${encodeURIComponent(midnight)}`, scope),
    enabled: workspaceId !== "",
  });
  const failed = useQuery({
    queryKey: ["home-runs-failed", workspaceId, weekAgo],
    queryFn: () => api<RunResponse[]>(`/api/v1/runs?status=failed&since=${encodeURIComponent(weekAgo)}`, scope),
    enabled: workspaceId !== "",
  });
  const recent = useQuery({
    queryKey: ["home-runs-recent", workspaceId],
    queryFn: () => api<RunResponse[]>("/api/v1/runs?limit=6", scope),
    enabled: workspaceId !== "",
  });
  const agents = useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: () => api<AgentResponse[]>("/api/v1/agents", scope),
    enabled: workspaceId !== "",
  });
  const inbox = useInboxCount();
  const agentName = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));

  function duration(run: RunResponse): string {
    const total = seconds(run);
    if (total === null) return "—";
    if (total < 60) return fill(t("durationSeconds"), { n: String(total) });
    return fill(t("durationMinutes"), { m: String(Math.floor(total / 60)), s: String(total % 60) });
  }

  // A time alone reads as today, and this page's lists reach back further
  // than that — last night's run at 21:13 would look like it just finished.
  function clock(value: string): string {
    const at = new Date(value);
    const time = at.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
    if (at.toDateString() === new Date().toDateString()) return time;
    // Spelled from the catalogue rather than `toLocaleDateString`: ICU builds
    // disagree on zh-CN (Chrome 10月25日, Node 10/25).
    return `${fill(t("dateShort"), { m: String(at.getMonth() + 1), d: String(at.getDate()) })} ${time}`;
  }

  function titleOf(run: RunResponse): string {
    return run.input_preview?.trim() || run.id.slice(0, 8);
  }

  const recentFailures = (failed.data ?? []).filter(
    (run) => Date.now() - Date.parse(run.finished_at ?? run.created_at) < DAY,
  );
  const inboxWaiting = inbox !== null && inbox !== 0;
  const todoReady = failed.data !== undefined && inbox !== null;

  const finished = (today.data ?? []).filter((run) => run.finished_at !== null);
  const completed = (today.data ?? []).filter((run) => run.status === "completed");
  const timed = completed.map(seconds).filter((value): value is number => value !== null);

  return (
    <div className="home">
      <PageHeading
        title={t("overview")}
        intro={t("overviewIntro")}
        extra={writer ? (
          <div className="home-actions">
            <Button onClick={() => navigate(`${base}/agents`)}>{t("homeNewAgent")}</Button>
            <Button type="primary" onClick={() => navigate(`${base}/runs`)}>{t("homeSubmitRun")}</Button>
          </div>
        ) : null}
      />

      <Panel title={t("homeTodo")} action={<Link to={`${base}/inbox`}>{t("homeAllTodo")}</Link>}>
        {failed.isError ? (
          <Retry error={failed.error} onRetry={() => void failed.refetch()} />
        ) : !todoReady ? (
          <p className="home-muted home-pad">{t("loading")}</p>
        ) : recentFailures.length === 0 && !inboxWaiting ? (
          <p className="home-muted home-pad">{t("homeTodoEmpty")}</p>
        ) : (
          <ul className="home-list">
            {recentFailures.map((run) => (
              <li key={run.id}>
                <Link className="home-row" to={`${base}/runs/${run.id}`}>
                  <span className="home-pill home-pill-bad">{t("homeFailed")}</span>
                  <span className="home-row-main">
                    <span className="home-row-title">{titleOf(run)}</span>
                    <span className="home-row-meta">
                      {[agentName.get(run.agent_id ?? ""), run.failure_reason, clock(run.finished_at ?? run.created_at)]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </span>
                </Link>
              </li>
            ))}
            {inboxWaiting ? (
              <li>
                <Link className="home-row" to={`${base}/inbox`}>
                  <span className="home-pill home-pill-wait">{t("navInbox")}</span>
                  <span className="home-row-main">
                    <span className="home-row-title">
                      {inbox === "?" ? t("homeInboxUnknown") : fill(t("homeInboxCount"), { count: String(inbox) })}
                    </span>
                    <span className="home-row-meta">{t("navInboxIntro")}</span>
                  </span>
                </Link>
              </li>
            ) : null}
          </ul>
        )}
      </Panel>

      <section className="home-stats" aria-label={t("homeToday")}>
        <Stat label={t("homeTodayRuns")} query={today} value={() => String(today.data!.length)} />
        <Stat
          label={t("homeSuccessRate")}
          query={today}
          value={() => (finished.length === 0 ? "—" : `${Math.round((completed.length / finished.length) * 100)}%`)}
          note={finished.length === 0 ? t("homeNothingFinished") : fill(t("homeSuccessNote"), { done: String(completed.length), all: String(finished.length) })}
        />
        <Stat
          label={t("homeAverageTime")}
          query={today}
          value={() => (timed.length === 0 ? "—" : fill(t("durationSeconds"), { n: String(Math.round(timed.reduce((a, b) => a + b, 0) / timed.length)) }))}
          note={t("homeAverageNote")}
        />
        <Stat label={t("homeFailedWeek")} query={failed} value={() => String(failed.data!.length)} />
      </section>

      <div className="home-columns">
        <Panel title={t("homeRecent")} action={<Link to={`${base}/runs`}>{t("homeAllRuns")}</Link>} wide>
          {recent.isError ? (
            <Retry error={recent.error} onRetry={() => void recent.refetch()} />
          ) : recent.data === undefined ? (
            <p className="home-muted home-pad">{t("loading")}</p>
          ) : recent.data.length === 0 ? (
            <p className="home-muted home-pad">{t("emptyRuns")}</p>
          ) : (
            <ul className="home-list">
              {recent.data.map((run) => (
                <li key={run.id}>
                  <Link className="home-row home-row-run" to={`${base}/runs/${run.id}`}>
                    <span className="home-row-main">
                      <span className="home-row-title">{titleOf(run)}</span>
                      <span className="home-row-meta">{agentName.get(run.agent_id ?? "") ?? ""}</span>
                    </span>
                    <StatusTag code={run.status} />
                    <span className="home-num">{duration(run)}</span>
                    <span className="home-num">{clock(run.created_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <div className="home-side">
          <Panel title={t("agents")} action={<Link to={`${base}/agents`}>{t("homeAllAgents")}</Link>}>
            {agents.isError ? (
              <Retry error={agents.error} onRetry={() => void agents.refetch()} />
            ) : (
              <ul className="home-list">
                {(agents.data ?? []).slice(0, 5).map((agent) => (
                  <li key={agent.id}>
                    <Link className="home-row" to={`${base}/agents/${agent.id}`}>
                      <span className="home-avatar" aria-hidden="true">{agent.name.slice(0, 1).toUpperCase()}</span>
                      <span className="home-row-main">
                        <span className="home-row-title">{agent.name}</span>
                        <span className="home-row-meta">{t(agent.current_version_id === null ? "homeAgentDraft" : "homeAgentPublished")}</span>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          {writer ? <SetupSteps base={base} agents={agents.data} /> : null}
        </div>
      </div>
    </div>
  );
}

function Panel({ title, action, wide = false, children }: { title: string; action?: ReactNode; wide?: boolean; children: ReactNode }) {
  const id = useId();
  return (
    <section className={`home-panel${wide ? " home-panel-wide" : ""}`} aria-labelledby={id}>
      <div className="home-panel-head">
        <h2 id={id}>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function Stat({ label, query, value, note }: {
  label: string;
  query: { isError: boolean; data: unknown };
  value: () => string;
  note?: string;
}) {
  const { t } = useLocale();
  return (
    <div className="home-stat">
      <span className="home-stat-label">{label}</span>
      <b className="home-stat-value">{query.isError ? "?" : query.data === undefined ? "…" : value()}</b>
      {query.isError ? <span className="home-stat-note">{t("homeStatUnavailable")}</span> : note === undefined ? null : <span className="home-stat-note">{note}</span>}
    </div>
  );
}

function Retry({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useLocale();
  return (
    <div className="home-pad">
      <Alert type="error" title={problemMessage(error, t)} action={<Button onClick={onRetry}>{t("retry")}</Button>} />
    </div>
  );
}

type Step = { key: string; label: MessageKey; to: string; done: boolean | null };

/**
 * The setup steps not yet done, and nothing once they all are. A step whose
 * answer could not be read is left out rather than shown as undone: telling
 * someone to connect a model they have connected is worse than saying less.
 */
function SetupSteps({ base, agents }: { base: string; agents: AgentResponse[] | undefined }) {
  const { t } = useLocale();
  const workspaceId = base.split("/")[2] ?? "";
  const scope = { workspace: workspaceId };
  const endpoints = useQuery({
    queryKey: ["model-endpoints", workspaceId],
    queryFn: () => api<unknown[]>("/api/v1/model-endpoints", scope),
    retry: false,
  });
  const bindings = useQuery({
    queryKey: ["channel-bindings", workspaceId],
    queryFn: () => api<unknown[]>("/api/v1/channel-bindings", scope),
    retry: false,
  });
  const members = useQuery({
    queryKey: ["members", workspaceId],
    queryFn: () => api<unknown[]>(`/api/v1/workspaces/${workspaceId}/members`, scope),
    retry: false,
  });
  const known = (data: unknown[] | undefined, test: (rows: unknown[]) => boolean) => (data === undefined ? null : test(data));
  const steps: Step[] = [
    { key: "model", label: "homeSetupModel", to: `${base}/settings#model-endpoints`, done: known(endpoints.data, (rows) => rows.length > 0) },
    { key: "agent", label: "homeSetupAgent", to: `${base}/agents`, done: agents === undefined ? null : agents.some((agent) => agent.current_version_id !== null) },
    { key: "channel", label: "homeSetupChannel", to: `${base}/channels`, done: known(bindings.data, (rows) => rows.length > 0) },
    { key: "members", label: "homeSetupMembers", to: `${base}/settings#members`, done: known(members.data, (rows) => rows.length > 1) },
  ];
  const undone = steps.filter((step) => step.done === false);
  if (undone.length === 0) return null;
  return (
    <Panel title={t("homeSetup")}>
      <ul className="home-list">
        {undone.map((step) => (
          <li key={step.key}>
            <Link className="home-row" to={step.to}>
              <span className="home-step" aria-hidden="true" />
              <span className="home-row-main"><span className="home-row-title">{t(step.label)}</span></span>
            </Link>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

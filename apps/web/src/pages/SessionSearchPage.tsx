import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Input, Space, Tag, Typography } from "antd";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { SearchHitResponse } from "../api/types";
import { useT } from "../i18n/locale";
import { shortenId } from "../tables/ShortId";
import { EmptyState } from "../ui/EmptyState";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

export function SessionSearchPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const [query, setQuery] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const hits = useQuery({
    queryKey: ["session-search", workspaceId, asked],
    queryFn: () => api<SearchHitResponse[]>(`/api/v1/memories/search?q=${encodeURIComponent(asked ?? "")}`, { workspace: workspaceId ?? "" }),
    enabled: workspaceId !== null && asked !== null && asked !== "",
  });
  function search() {
    const value = query.trim();
    if (!value) return;
    if (value === asked) void hits.refetch();
    else setAsked(value);
  }
  return (
      <Card variant="borderless" className="page-alert">
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary">{t("searchSessionsIntro")}</Typography.Paragraph>
          <Space wrap>
            <Input
              aria-label={t("searchSessions")}
              style={{ width: "min(320px, 100%)" }}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onPressEnter={(event) => { if (!event.nativeEvent.isComposing) search(); }}
            />
            <Button loading={hits.isFetching} disabled={query.trim() === ""} onClick={search}>
              {t("searchRun")}
            </Button>
          </Space>
          {hits.isError ? (
            <Alert type="warning" showIcon message={problemMessage(hits.error, t)} />
          ) : null}
          {asked !== null && hits.isSuccess && (hits.data ?? []).length === 0 && !hits.isFetching ? (
            <EmptyState title={t("searchNoHits")} />
          ) : null}
          {(hits.data ?? []).map((hit) => (
            <Card key={`${hit.session_id}-${hit.sequence}`} variant="borderless" className="page-alert session-search-result">
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Space wrap>
                  <Tag>{hit.role}</Tag>
                  <Link to={`/workspaces/${workspaceId}/records?session=${hit.session_id}#subjects`}>{t("sessionSubject")}</Link>
                  {hit.run_id === null ? null : (
                    <Link title={hit.run_id} to={`/workspaces/${workspaceId}/runs/${hit.run_id}`}>{shortenId(hit.run_id)}</Link>
                  )}
                </Space>
                <Typography.Paragraph>{hit.snippet}</Typography.Paragraph>
                {hit.shortened ? (
                  // Said, not implied by an ellipsis: a reader who does not
                  // know they are holding part of a message reads it as the
                  // whole of one.
                  <Typography.Text type="secondary">{t("searchShortened")}</Typography.Text>
                ) : null}
              </Space>
            </Card>
          ))}
        </Space>
      </Card>

  );
}

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Button, Typography } from "antd";
import { ApiError, api, asApiError } from "../api/client";
import { useT } from "../i18n/locale";

type SavedFile = { path: string; size_bytes: number };
type Snapshot = { revision_id: string | null; items: SavedFile[] };

export function SavedFiles({ workspaceId, runId, refreshToken }: {
  workspaceId: string; runId: string | null;
  refreshToken?: string | number | null | undefined;
}) {
  const t = useT();
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const base = `/api/v1/runs/${runId ?? ""}/files`;
  const files = useQuery({
    queryKey: ["run-saved-files", workspaceId, runId, refreshToken],
    queryFn: () => api<Snapshot>(base, { workspace: workspaceId }),
    enabled: runId !== null && workspaceId !== "",
  });
  async function save(file: SavedFile) {
    const revision = files.data?.revision_id;
    if (!revision) return;
    setPending(file.path);
    setNotice(null);
    try {
      const params = new URLSearchParams({ revision_id: revision, path: file.path });
      const response = await fetch(`${base}/content?${params}`, {
        credentials: "include",
        headers: { "X-Workspace-Id": workspaceId },
      });
      if (!response.ok) throw await asApiError(response);
      const url = URL.createObjectURL(await response.blob());
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = file.path.split("/").pop() ?? "download";
        link.click();
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setNotice(t("savedFilesChanged"));
        await files.refetch();
      } else {
        setNotice(t("savedFilesDownloadFailed"));
      }
    } finally {
      setPending(null);
    }
  }
  if (runId === null) return null;
  return (
    <section className="saved-files" aria-label={t("savedFilesTitle")}>
      <Typography.Title level={5}>{t("savedFilesTitle")}</Typography.Title>
      <p>{t("savedFilesHint")}</p>
      {notice && <Alert type="warning" showIcon title={notice} />}
      {files.isError ? <Alert type="error" showIcon title={t("savedFilesLoadFailed")} /> : null}
      <Button onClick={() => { setNotice(null); void files.refetch(); }} loading={files.isFetching}>{t("savedFilesRefresh")}</Button>
      {files.isSuccess && files.data.items.length === 0 ? <p>{t("savedFilesEmpty")}</p> : null}
      <ul className="saved-files-list">
        {(files.data?.items ?? []).map((file) => (
          <li key={file.path} className="saved-file-row">
            <span className="saved-file-name">{file.path}</span>
            <span>{file.size_bytes.toLocaleString()} B</span>
            <Button aria-label={`${t("savedFileDownload")} ${file.path}`} disabled={pending !== null} onClick={() => void save(file)}>
              {pending === file.path ? t("savedFileDownloading") : t("savedFileDownload")}
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api, asApiError } from "../api/client";
import { useT } from "../i18n/locale";

export type SavedFile = { path: string; size_bytes: number };
type Snapshot = { revision_id: string | null; items: SavedFile[] };

/**
 * The session's saved files and the one way to download them.
 *
 * Shared by the files panel and the cards in the transcript, so a card for a
 * file the Agent sent and the panel's row for it are the same listing and
 * the same download — a download is pinned to the listing's revision, and two
 * listings could disagree about which one that is.
 */
export function useSavedFiles(sessionId: string | null, refreshToken?: string | number | null) {
  const t = useT();
  // Remembered with the session it was about, so switching sessions does not
  // carry one session's "download failed" over to the next.
  const [said, setSaid] = useState<{ sessionId: string | null; text: string } | null>(null);
  const notice = said !== null && said.sessionId === sessionId ? said.text : null;
  const setNotice = (text: string | null) => setSaid(text === null ? null : { sessionId, text });
  const [pending, setPending] = useState<string | null>(null);
  const base = `/api/v1/end-user/sessions/${sessionId ?? ""}/files`;
  const files = useQuery({
    queryKey: ["session-saved-files", sessionId, refreshToken],
    queryFn: () => api<Snapshot>(base),
    enabled: sessionId !== null,
  });

  async function save(path: string): Promise<void> {
    const revision = files.data?.revision_id;
    if (!revision) return;
    setPending(path);
    setNotice(null);
    try {
      const params = new URLSearchParams({ revision_id: revision, path });
      const response = await fetch(`${base}/content?${params}`, { credentials: "include" });
      if (!response.ok) throw await asApiError(response);
      const url = URL.createObjectURL(await response.blob());
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = path.split("/").pop() ?? "download";
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

  return { files, save, pending, notice, setNotice };
}

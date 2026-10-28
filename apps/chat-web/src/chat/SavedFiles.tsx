import { sizeOf } from "./fileSize";
import type { useSavedFiles } from "./useSavedFiles";
import { useT } from "../i18n/locale";

export function SavedFiles({ saved }: { saved: ReturnType<typeof useSavedFiles> }) {
  const t = useT();
  const { files, save, pending, notice, setNotice } = saved;
  const items = files.data?.items ?? [];
  // Most conversations never save a file; a panel saying so under each one
  // is noise. A failed listing or a download notice still shows, since those
  // are things the person needs to act on.
  if (items.length === 0 && !files.isError && notice === null) return null;
  return (
    <section className="saved-files" aria-label={t("savedFilesTitle")}>
      <div className="saved-files-head">
        <h3>{t("savedFilesTitle")}</h3>
        <button
          type="button"
          className="saved-files-refresh"
          onClick={() => { setNotice(null); void files.refetch(); }}
          disabled={files.isFetching}
        >
          {t("savedFilesRefresh")}
        </button>
      </div>
      {notice && <p className="saved-files-notice" role="alert">{notice}</p>}
      {files.isError ? <p className="saved-files-notice" role="alert">{t("savedFilesLoadFailed")}</p> : null}
      <ul className="saved-files-list">
        {items.map((file) => {
          const slash = file.path.lastIndexOf("/");
          const name = file.path.slice(slash + 1);
          const folder = slash < 0 ? "" : file.path.slice(0, slash + 1);
          const extension = name.includes(".") ? (name.split(".").pop() ?? "").slice(0, 4).toUpperCase() : "";
          return (
            <li key={file.path} className="file-card" role="group" aria-label={name} title={file.path}>
              <span className="file-card-kind" aria-hidden="true">{extension || "FILE"}</span>
              <span className="file-card-text">
                <span className="file-card-name">{name}</span>
                <span className="file-card-meta">{folder === "" ? sizeOf(file.size_bytes) : `${folder} · ${sizeOf(file.size_bytes)}`}</span>
              </span>
              <button
                type="button"
                aria-label={`${t("savedFileDownload")} ${file.path}`}
                disabled={pending !== null}
                onClick={() => void save(file.path)}
              >
                {pending === file.path ? t("savedFileDownloading") : t("savedFileDownload")}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

import type { useSavedFiles } from "./useSavedFiles";
import { useT } from "../i18n/locale";

export function SavedFiles({ saved }: { saved: ReturnType<typeof useSavedFiles> }) {
  const t = useT();
  const { files, save, pending, notice, setNotice } = saved;
  return (
    <section className="saved-files" aria-label={t("savedFilesTitle")}>
      <h3>{t("savedFilesTitle")}</h3>
      <p>{t("savedFilesHint")}</p>
      {notice && <p role="alert">{notice}</p>}
      {files.isError ? <p role="alert">{t("savedFilesLoadFailed")}</p> : null}
      <button type="button" onClick={() => { setNotice(null); void files.refetch(); }} disabled={files.isFetching}>{t("savedFilesRefresh")}</button>
      {files.isSuccess && files.data.items.length === 0 ? <p>{t("savedFilesEmpty")}</p> : null}
      <ul className="saved-files-list">
        {(files.data?.items ?? []).map((file) => (
          <li key={file.path} className="saved-file-row">
            <span className="saved-file-name">{file.path}</span>
            <span>{file.size_bytes.toLocaleString()} B</span>
            <button type="button" aria-label={`${t("savedFileDownload")} ${file.path}`} disabled={pending !== null} onClick={() => void save(file.path)}>
              {pending === file.path ? t("savedFileDownloading") : t("savedFileDownload")}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

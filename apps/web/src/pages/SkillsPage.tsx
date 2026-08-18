import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Modal, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { api, apiWithStatus } from "../api/client";
import { problemMessage } from "../api/messages";
import type { SkillFilePayload, SkillResponse, SkillVersionResponse } from "../api/types";
import { useT } from "../i18n/locale";
import type { MessageKey } from "../i18n/zh-CN";
import { useWorkspaceId } from "../workspace/useWorkspaceId";

const SOURCES: Record<string, MessageKey> = {
  upload: "skillSourceUpload",
  git: "skillSourceGit",
  proposal: "skillSourceProposal",
};

/**
 * Reads a chosen directory into the file list the API takes.
 *
 * The directory picker hands over `File` objects with `webkitRelativePath`
 * set; its first segment is the folder the person picked and is dropped, so
 * uploading `rollout/SKILL.md` stores `SKILL.md`. A plain multi-file selection
 * has no relative path and keeps its own name.
 *
 * The browser reads the files and sends a list. Nothing here builds an
 * archive, which is red line three on the manual path: the server never grows
 * a face that unpacks one.
 */
async function readFiles(files: File[]): Promise<SkillFilePayload[]> {
  return Promise.all(
    files.map(async (file) => ({
      path: relativePath(file),
      content: await file.text(),
    })),
  );
}

function relativePath(file: File): string {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
  if (relative === undefined || relative === "") {
    return file.name;
  }
  const segments = relative.split("/");
  return segments.length > 1 ? segments.slice(1).join("/") : relative;
}

export function SkillsPage() {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const queryClient = useQueryClient();
  const [modal, contextHolder] = Modal.useModal();
  const [importForm] = Form.useForm<{ url: string }>();
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const scope = { workspace: workspaceId ?? "" };
  const listQuery = ["skills", workspaceId] as const;

  const listed = useQuery({
    queryKey: listQuery,
    queryFn: () => api<SkillResponse[]>("/api/v1/skills", scope),
    enabled: workspaceId !== null,
  });

  function refresh(): void {
    setError(null);
    void queryClient.invalidateQueries({ queryKey: listQuery });
    void queryClient.invalidateQueries({ queryKey: ["skill-versions"] });
  }

  const upload = useMutation({
    mutationFn: async (files: File[]) =>
      api<SkillResponse>("/api/v1/skills", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ scope: "workspace", files: await readFiles(files) }),
      }),
    onSuccess: () => {
      setNote(null);
      refresh();
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  const importing = useMutation({
    mutationFn: (url: string) =>
      api<SkillResponse>("/api/v1/skills/import", {
        ...scope,
        method: "POST",
        body: JSON.stringify({ scope: "workspace", url }),
      }),
    onSuccess: () => {
      importForm.resetFields();
      setNote(null);
      refresh();
    },
    onError: (caught) => setError(problemMessage(caught)),
  });

  if (listed.isError) {
    return (
      <Alert
        type="error"
        title={problemMessage(listed.error)}
        action={<Button onClick={() => void listed.refetch()}>{t("retry")}</Button>}
        showIcon
      />
    );
  }

  const skills = listed.data ?? [];
  const platform = skills.filter((skill) => skill.scope === "platform");
  const mine = skills.filter((skill) => skill.scope === "workspace");

  return (
    <>
      {contextHolder}
      <div className="page-heading">
        <div>
          <Typography.Title level={2}>{t("skillsTitle")}</Typography.Title>
          <Typography.Paragraph type="secondary">{t("skillsIntro")}</Typography.Paragraph>
        </div>
      </div>
      {error === null ? null : (
        <Alert className="page-alert" type="warning" title={error} showIcon />
      )}
      {note === null ? null : (
        <Alert className="page-alert" type="success" title={note} showIcon />
      )}

      <Card title={t("uploadSkill")} variant="borderless" className="page-alert">
        <Typography.Paragraph type="secondary">{t("uploadSkillHint")}</Typography.Paragraph>
        <input
          type="file"
          multiple
          aria-label={t("chooseFiles")}
          disabled={upload.isPending}
          onChange={(event) => {
            const chosen = Array.from(event.target.files ?? []);
            if (chosen.length > 0) {
              upload.mutate(chosen);
            }
            event.target.value = "";
          }}
        />
      </Card>

      <Card title={t("importSkill")} variant="borderless" className="page-alert">
        <Typography.Paragraph type="secondary">{t("importSkillHint")}</Typography.Paragraph>
        <Form<{ url: string }>
          form={importForm}
          layout="inline"
          requiredMark={false}
          onFinish={(values) => importing.mutate(values.url)}
        >
          <Form.Item
            name="url"
            label={t("importSkillUrl")}
            rules={[{ required: true, whitespace: true, message: t("required") }]}
          >
            <Input style={{ minWidth: 320 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={importing.isPending}>
              {t("importSkill")}
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card
        title={t("workspaceSkills")}
        variant="borderless"
        className="page-alert"
        loading={listed.isPending}
      >
        {mine.length === 0 ? (
          <Empty description={t("emptySkills")} />
        ) : (
          mine.map((skill) => (
            <SkillRow
              key={skill.id}
              skill={skill}
              editable
              onChanged={refresh}
              onError={setError}
              onNote={setNote}
              confirm={modal.confirm}
            />
          ))
        )}
      </Card>

      <Card title={t("platformSkills")} variant="borderless" loading={listed.isPending}>
        <Typography.Paragraph type="secondary">{t("platformSkillsReadOnly")}</Typography.Paragraph>
        {platform.length === 0 ? (
          <Empty description={t("emptySkills")} />
        ) : (
          platform.map((skill) => (
            <SkillRow
              key={skill.id}
              skill={skill}
              editable={false}
              onChanged={refresh}
              onError={setError}
              onNote={setNote}
              confirm={modal.confirm}
            />
          ))
        )}
      </Card>
    </>
  );
}

type RowProps = {
  skill: SkillResponse;
  /**
   * Whether this workspace may change the skill. A platform skill read from a
   * workspace renders without the buttons rather than with disabled ones: a
   * control that does not move asks the reader to work out why for themselves.
   */
  editable: boolean;
  onChanged: () => void;
  onError: (message: string) => void;
  onNote: (message: string | null) => void;
  confirm: ReturnType<typeof Modal.useModal>[0]["confirm"];
};

function SkillRow({ skill, editable, onChanged, onError, onNote, confirm }: RowProps) {
  const t = useT();
  const workspaceId = useWorkspaceId();
  const scope = { workspace: workspaceId ?? "" };
  const versionsQuery = ["skill-versions", skill.id] as const;

  const versions = useQuery({
    queryKey: versionsQuery,
    queryFn: () => api<SkillVersionResponse[]>(`/api/v1/skills/${skill.id}/versions`, scope),
    enabled: workspaceId !== null,
  });

  const reimport = useMutation({
    mutationFn: (url: string) =>
      apiWithStatus<SkillVersionResponse>(`/api/v1/skills/${skill.id}/import`, {
        ...scope,
        method: "POST",
        body: JSON.stringify({ url }),
      }),
    onSuccess: (result) => {
      // 200 means this content was already a version. Saying so is the point
      // of having two status codes: re-importing an unchanged repository is
      // not a publication, and a silent success reads like one.
      onNote(result.status === 200 ? t("skillUnchanged") : null);
      onChanged();
    },
    onError: (caught) => onError(problemMessage(caught)),
  });

  const withdraw = useMutation({
    mutationFn: (versionId: string) =>
      api<SkillVersionResponse>(`/api/v1/skills/${skill.id}/versions/${versionId}/withdraw`, {
        ...scope,
        method: "POST",
      }),
    onSuccess: onChanged,
    onError: (caught) => onError(problemMessage(caught)),
  });

  const makeCurrent = useMutation({
    mutationFn: (versionId: string) =>
      api<SkillResponse>(`/api/v1/skills/${skill.id}/current-version`, {
        ...scope,
        method: "PUT",
        body: JSON.stringify({ version_id: versionId }),
      }),
    onSuccess: onChanged,
    onError: (caught) => onError(problemMessage(caught)),
  });

  const rows = versions.data ?? [];
  const latestUrl = rows.find((item) => item.source_url !== null)?.source_url ?? null;

  return (
    <article className="workspace-row">
      <div className="workspace-summary">
        <Typography.Title level={4}>{skill.name}</Typography.Title>
        <Space wrap>
          <Tag>{skill.scope === "platform" ? t("platformSkills") : t("workspaceSkills")}</Tag>
          {editable && latestUrl !== null ? (
            <Button
              size="small"
              loading={reimport.isPending}
              onClick={() => reimport.mutate(latestUrl)}
            >
              {t("importSkill")}
            </Button>
          ) : null}
        </Space>
        <Typography.Paragraph type="secondary">{t("skillVersions")}</Typography.Paragraph>
        {rows.map((version) => (
          <Space key={version.id} wrap className="skill-version-row">
            <Typography.Text>
              {t("skillVersion").replace("{number}", String(version.version_number))}
            </Typography.Text>
            <Tag>{t(SOURCES[version.source] ?? "skillSourceUpload")}</Tag>
            <Typography.Text type="secondary">{version.description}</Typography.Text>
            {version.id === skill.current_version_id ? (
              <Tag color="green">{t("skillCurrentVersion")}</Tag>
            ) : null}
            {version.status === "withdrawn" ? <Tag>{t("skillWithdrawn")}</Tag> : null}
            {version.findings.some((finding) => finding.severity === "blocking") ? (
              <Tag color="red">{t("skillBlocked")}</Tag>
            ) : null}
            {editable && version.bindable && version.id !== skill.current_version_id ? (
              <Button
                size="small"
                loading={makeCurrent.isPending}
                onClick={() => makeCurrent.mutate(version.id)}
              >
                {t("skillMakeCurrent")}
              </Button>
            ) : null}
            {editable && version.status === "active" ? (
              <Button
                size="small"
                loading={withdraw.isPending}
                onClick={() =>
                  void confirm({
                    title: t("skillWithdraw"),
                    content: t("skillWithdrawWarning"),
                    okText: t("confirm"),
                    cancelText: t("cancel"),
                    onOk: () => withdraw.mutateAsync(version.id).catch(() => undefined),
                  })
                }
              >
                {t("skillWithdraw")}
              </Button>
            ) : null}
          </Space>
        ))}
      </div>
    </article>
  );
}

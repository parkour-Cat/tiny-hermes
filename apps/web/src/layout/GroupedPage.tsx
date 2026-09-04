import { Anchor, Typography } from "antd";
import type { ReactNode } from "react";

import { NAV_GROUPS, visibleSections } from "./navigation";
import { useAuth } from "../auth/AuthProvider";
import { useT } from "../i18n/locale";
import { useMyRole } from "../workspace/useMyRole";

/**
 * 一个合并入口下面的若干段。
 *
 * **前端隐藏不构成授权。** 后端的拒绝仍然是唯一的权限判据；这里少画一段只是
 * 不去邀请一次注定失败的点击。谁把这段注释删掉之前，先想清楚下一个读代码的人
 * 会不会以为隐藏就是授权。
 *
 * 角色未知时一段都不画，而不是先画全部再删：后者会让一个 viewer 看到一次闪现
 * 的、他其实点不动的入口列表。
 */
export function GroupedPage({
  groupKey,
  render,
}: {
  groupKey: string;
  render: (sectionKey: string) => ReactNode;
}) {
  const t = useT();
  const auth = useAuth();
  const { role } = useMyRole();
  const group = NAV_GROUPS.find((candidate) => candidate.key === groupKey);
  if (group === undefined || role === null) return null;

  const visible = visibleSections(group, role, auth.user?.is_platform_admin === true);

  return (
    <div className="grouped-page">
      <Typography.Title level={2}>{t(group.labelKey)}</Typography.Title>
      <Typography.Paragraph type="secondary">{t(group.introKey)}</Typography.Paragraph>
      <Anchor
        affix={false}
        direction="horizontal"
        items={visible.map((section) => ({
          key: section.key,
          href: `#${section.key}`,
          title: t(section.labelKey),
        }))}
      />
      {visible.map((section) => (
        <section key={section.key} id={section.key} className="grouped-section">
          <Typography.Title level={4}>{t(section.labelKey)}</Typography.Title>
          {section.introKey === null ? null : (
            <Typography.Paragraph type="secondary">{t(section.introKey)}</Typography.Paragraph>
          )}
          {render(section.key)}
        </section>
      ))}
    </div>
  );
}

import type { MessageKey } from "../i18n/zh-CN";
import type { Role } from "../workspace/useMyRole";

export type NavSection = {
  key: string;
  labelKey: MessageKey;
  /** 这一段自己的那句说明。`null` 表示这一页还没有——见 Task 14。 */
  introKey: MessageKey | null;
  /** 工作空间下的相对路径，不带 `/workspaces/:id/` 前缀。 */
  path: string;
  /** 能看见这一段的角色。`null` = 都能看见。`platform_admin` 总能看见。
   *  **隐藏不是权限**：后端照旧拒绝，这里只是不邀请一次注定失败的点击。 */
  roles: Role[] | null;
  /** 只有 `is_platform_admin` 标志为真的人才看得见。平台管理员不是一个 workspace
   *  角色，所以这一位和 `roles` 分开：一个兼任 workspace_admin 的平台管理员从
   *  `/members/me` 得到的是 `workspace_admin`，靠角色列表判不出他能看见这一段。 */
  platformAdminOnly?: boolean;
};

export type NavGroup = {
  key: string;
  labelKey: MessageKey;
  introKey: MessageKey;
  sections: NavSection[];
};

// 每一段的可见角色不是猜的：2026-09-04 用集成测试夹具，以 viewer 和 developer
// 身份逐个 GET 各页的列表接口，记下 200/403，再对照服务层的判定写下来源。
export const NAV_GROUPS: NavGroup[] = [
  {
    key: "agents",
    labelKey: "agents",
    introKey: "agentsIntro",
    // 依据：agents/application/service.py 的 READERS（viewer 200）
    sections: [{ key: "agents", labelKey: "agents", introKey: "agentsIntro", path: "agents", roles: null }],
  },
  {
    key: "runs",
    labelKey: "runs",
    introKey: "runsIntro",
    // 依据：runs/application/service.py 的 READERS（viewer 200）
    sections: [{ key: "runs", labelKey: "runs", introKey: "runsIntro", path: "runs", roles: null }],
  },
  {
    key: "channels",
    labelKey: "channels",
    introKey: "channelsIntro",
    // 依据：/api/v1/channel-bindings 对 viewer 403、developer 200
    //（channels 服务的 WRITERS = workspace_admin + developer）
    sections: [
      {
        key: "channels",
        labelKey: "channels",
        introKey: "channelsIntro",
        path: "channels",
        roles: ["workspace_admin", "developer"],
      },
    ],
  },
  {
    key: "inbox",
    labelKey: "navInbox",
    introKey: "navInboxIntro",
    sections: [
      // 依据：/api/v1/approvals 对 viewer 200
      { key: "approvals", labelKey: "approvals", introKey: "approvalsIntro", path: "approvals", roles: null },
      // 依据：/api/v1/skill-proposals 对 viewer 200
      { key: "proposals", labelKey: "proposals", introKey: "proposalsIntro", path: "skill-proposals", roles: null },
      // 依据：/api/v1/memories/pending 对 viewer 与 developer 都是 403
      //（memory 服务只让 workspace_admin 审）
      { key: "memory", labelKey: "memoryReview", introKey: "memoryReviewIntro", path: "memory", roles: ["workspace_admin"] },
    ],
  },
  {
    key: "tooling",
    labelKey: "navTooling",
    introKey: "navToolingIntro",
    sections: [
      // 依据：skills/application/service.py 的 READERS（viewer 200）
      { key: "skills", labelKey: "skills", introKey: "skillsIntro", path: "skills", roles: null },
      // 依据：http_tools/application/service.py 的 READERS（viewer 200）
      { key: "http-tools", labelKey: "httpTools", introKey: "httpToolsIntro", path: "http-tools", roles: null },
      // 依据：mcp/application/service.py 的 READERS（viewer 200）
      { key: "mcp-servers", labelKey: "mcpServers", introKey: "mcpServersIntro", path: "mcp-servers", roles: null },
    ],
  },
  {
    key: "records",
    labelKey: "navRecords",
    introKey: "navRecordsIntro",
    sections: [
      // 依据：/api/v1/audit-events 对 viewer 200
      { key: "audit", labelKey: "audit", introKey: "auditIntro", path: "audit", roles: null },
      // 依据：/api/v1/usage 对 viewer 200
      { key: "usage", labelKey: "usage", introKey: "usageIntro", path: "usage", roles: null },
      // 依据：memory/application/subject_service.py 的 STEWARDS = {workspace_admin}
      //（平台管理员另算，见 GroupedPage）
      { key: "subjects", labelKey: "subjectData", introKey: "subjectDataIntro", path: "subjects", roles: ["workspace_admin"] },
    ],
  },
  {
    key: "settings",
    labelKey: "navSettings",
    introKey: "navSettingsIntro",
    sections: [
      // 顺序按依赖：接模型之前得先有 Key，所以凭据保管箱在模型接入前面；出站范围
      // 是模型和工具都要过的门；程序用的 API 密钥和登录用的身份提供方放最后。
      // 依据：tenancy/application/workspace_service.py 的 READERS（viewer 200）
      { key: "members", labelKey: "members", introKey: "membersIntro", path: "members", roles: null },
      // 依据：/api/v1/secrets 对 viewer 403、developer 200
      { key: "secrets", labelKey: "secrets", introKey: "secretsIntro", path: "secrets", roles: ["workspace_admin", "developer"] },
      // 依据：/api/v1/model-endpoints 对 viewer 200（列出可选端点是所有成员的事）
      { key: "model-endpoints", labelKey: "modelEndpoints", introKey: "modelEndpointsIntro", path: "model-endpoints", roles: null },
      // 依据：outbound/application/service.py 的 READERS（viewer 200）
      { key: "outbound", labelKey: "outboundScopes", introKey: "outboundScopesIntro", path: "outbound", roles: null },
      // 依据：/api/v1/service-accounts 对 viewer 403、developer 200
      { key: "api-keys", labelKey: "apiKeys", introKey: "apiKeysIntro", path: "api-keys", roles: ["workspace_admin", "developer"] },
      // 依据：identity/application/oidc_service.py 的 _require_admin 看的是
      // is_platform_admin：viewer 与 developer 都 403，这不是 workspace 角色能开的门
      {
        key: "identity-providers",
        labelKey: "identityProviders",
        introKey: "identityProvidersIntro",
        path: "identity-providers",
        roles: null,
        platformAdminOnly: true,
      },
    ],
  },
];

/** 这个人看得见的段。`platform_admin` 角色（不是成员的平台管理员）看得见一切；
 *  `platformAdminOnly` 的段只看 `isPlatformAdmin` 标志。 */
export function visibleSections(
  group: NavGroup,
  role: Role,
  isPlatformAdmin: boolean,
): NavSection[] {
  return group.sections.filter((section) => {
    if (section.platformAdminOnly === true) return isPlatformAdmin;
    if (role === "platform_admin" || section.roles === null) return true;
    return section.roles.includes(role);
  });
}

export const zhCN = {
  appName: "tiny-hermes",
  appTagline: "轻量、可控的企业 Agent 运行平台",
  loading: "正在加载",
  retry: "重试",
  requestFailed: "请求失败，请稍后重试",
  networkFailed: "无法连接平台，请检查服务后重试",
  loginTitle: "登录管理控制台",
  email: "邮箱",
  password: "密码",
  login: "登录",
  loginFailed: "登录失败",
  bootstrapLink: "首次使用？初始化平台",
  bootstrapTitle: "创建首位平台管理员",
  bootstrapHint: "初始化只能成功一次。令牌不会保存到浏览器。",
  bootstrapToken: "初始化令牌",
  displayName: "显示名称",
  bootstrapSubmit: "完成初始化",
  bootstrapSucceeded: "初始化成功，请使用新账号登录",
  backToLogin: "返回登录",
  workspaceTitle: "工作空间",
  workspaceIntro: "每个工作空间独立保存成员、Agent 与运行数据。",
  newWorkspace: "新建工作空间",
  workspaceName: "名称",
  create: "创建",
  cancel: "取消",
  emptyWorkspaces: "还没有工作空间",
  workspaceActive: "正常",
  logout: "退出",
  signedInAs: "当前用户",
  required: "请填写此项",
  invalidEmail: "请输入有效邮箱",
  passwordMinimum: "密码至少 12 个字符",
  workspaceNameMaximum: "名称不能超过 120 个字符",
} as const;

export type MessageKey = keyof typeof zhCN;

export function t(key: MessageKey): string {
  return zhCN[key];
}

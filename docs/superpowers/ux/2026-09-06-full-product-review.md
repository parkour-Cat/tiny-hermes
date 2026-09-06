# tiny-hermes 全产品使用逻辑与 UI/UX 走查

日期：2026-09-06。状态：待讨论的调整建议，未作为已批准实施规格。基于当前本地工作区，含本日已完成的文案、间距和价格显示修复；本轮只新增走查文档，不修改产品、不发布新业务配置。

后续状态：用户已回复“开始吧”，授权按建议分批实施。上文保留走查当时的状态；实际改动及未完成项见 [实施进度](2026-09-06-implementation-progress.md)。

## 结论与优先级

共整理 **30 项调整事项**，另有 **3 项需专项验证的风险**。这是一轮专家走查，不是用户研究；“不易理解”属于有事实支撑的设计判断，不代表所有用户都会遇到。

主要问题是：配置缺少从准备到验证的完整流程；列表按内部模块组织；操作的显示与实际效果偶有差距；结果交付和数据可信度尚有缺口。继续逐句改文案或逐卡片调间距不能覆盖这些问题。

- P1（19 项）：优先排期，影响主要流程、操作判断或数据可信度；其中部分为代码确认，尚待真实场景复现。
- P2（11 项）：随后完善，影响查找效率、学习成本和一致性。
- 优先级不是安全漏洞分级，也不代表所有 P1 都需要同一批完成。建议按下文四批顺序处理。

## 走查范围与证据边界

| 范围 | 已覆盖内容 | 证据 |
| --- | --- | --- |
| 登录与初始化 | 本地登录、SSO 入口、初始化、无工作空间情况 | 页面与路由代码；未退出当前管理员或重新初始化 |
| 工作空间 | 列表、新建、进入、切换、角色影响 | 同日界面走查＋代码 |
| Agent | 列表、示例、空白创建、身份/模型/能力/对外配置、保存、发布、版本、回退入口 | 真实示例界面＋事件处理代码；本轮未提交修改 |
| 任务与调试 | 列表、提交表单、完成任务详情、工具与文件、时间线、调试会话 | 同日真实模型记录＋实际页面＋代码 |
| 渠道 | 空状态、飞书绑定表单与凭据选择、编辑流程、网页签发方 | 表单实际打开后取消＋代码；未新增渠道或发外部消息 |
| 待办 | 用户确认、管理员审批、审批历史、技能提案、记忆审核、会话搜索 | 实际空页面＋代码；无真实待批准事项 |
| 工具与技能 | 文件上传控件、归档导入、技能版本、HTTP 登记、MCP 登记/重读/撤回、绑定入口 | 空目录实测＋版本/操作代码；未上传或登记资源 |
| 记录 | 审计查询/导出、用量、用户数据查找/导出/擦除/记忆更正 | 真实记录页＋代码；未执行擦除 |
| 设置 | 成员、凭据、模型与价格、出站范围、API 密钥、控制台登录 | 当前管理员页面＋各页处理逻辑；未改密钥或权限 |
| 聊天端 | 接入等待页、路由、会话列表、输入、审批、导出、默认 Agent、个人数据、退出 | 等待页实测；正常对话/设置为代码核对，没有终端用户登录态 |
| 视觉 | 导航、标题、卡片、空状态、表单、表格、深浅主题 | 同日实际截图和尺寸测量；不等于全设备无障碍验收 |

本次桌面视口 1088×1038。待办内容高 2651px（约 2.6 屏）、5 个空状态；工具页高 3432px（约 3.3 屏）、3 个 HTML 表单，另有文件上传控件。数值是当前空数据、当前视口的测量，不能泛化为所有设备。

证据标记：**实测**为已看到或已操作验证的界面；**代码核对**为当前实现能直接确认的行为；**同日真实运行记录**来自本日此前的模型测试。每项建议和验收目标均是未来设计，不是当前能力声明。

## 建议的使用主线

1. 首次使用：选择工作空间 → 接入并验证模型 → 从示例或空白创建 Agent → 保存/发布 → 发消息试跑 → 获取答复与文件。
2. 增加能力：从 Agent 选择所需能力 → 缺少时进入能力库添加 → 解析/校验 → 绑定确切版本 → 发布 → 验证一次任务。
3. 对外发布：选择飞书或网页聊天 → 按场景配置 → 检查用户授权与 Agent 开放范围 → 验证真实收发 → 显示运行状态。
4. 日常处理：待我处理 → 看懂具体请求 → 批准/拒绝 → 看到任务继续或结束；等待他人和历史分别查看。
5. 查问题：按 Agent/时间/状态找任务 → 先看结果和失败原因 → 查看关联工具与事件 → 按需查看审计或用量。
6. 用户数据：从会话或受限查找定位终端用户 → 核实对象与范围 → 导出/更正/删除 → 明确反馈结果。

## 导航调整草案

| 建议一级入口 | 二级内容/默认视图 | 现有内容去向 |
| --- | --- | --- |
| 工作空间概况 | 简洁运行状态、待处理数、继续配置入口 | 新增轻量入口，数据不足时不展示虚构统计 |
| Agents | Agent 列表；详情内含配置、试用、版本、共享记忆、接入情况 | Agents、Playground、主动写共享记忆 |
| 任务与会话 | 任务列表、会话搜索；详情先结果后过程 | Runs、会话搜索；记录关系需后端支持 |
| 待办 | 待我处理／等待他人／处理历史，按类型筛选 | 审批、技能提案、记忆审核 |
| 能力库 | 技能／外部工具（HTTP、MCP），资源列表优先 | 工具与技能；名称可与用户再验证，不强求改名 |
| 接入与发布 | 飞书机器人／网页聊天；各自配置与验证 | 渠道绑定、网页签发方、相关 Agent 开放条件 |
| 运营记录 | 操作日志／用量与费用／用户数据，独立页面可直达 | 原记录页拆成二级页面 |
| 工作空间设置 | 成员／空间凭据／程序访问／允许的外部访问 | 原设置中的空间范围配置 |
| 平台管理（独立区域） | 模型服务、平台凭据与出站批准、控制台登录 | 全平台设置，只对有权角色开放 |

这是建议结构，不是照搬每个内部模块增加一级菜单。相同对象保留一个权威编辑入口，其他位置使用摘要＋跳转；例如 Agent 内可发起接入配置，但渠道对象只维护一份。

现有产品设计 §20 原本已要求工作空间切换器、渐进展示、明确草稿/已发布状态、固定保存/发布操作。重新设计应先落实这些原则。导航命名与分组的变化需要更新规格；不能顺便改掉权限、审批或版本不可变规则。

## 调整清单

| ID | 区域 | 优先级 | 问题 |
| --- | --- | --- | --- |
| UX-01 | 全局导航 | P1 | [页面按模块拼接，缺少任务主线](#ux-01) |
| UX-02 | 设置 | P1 | [平台配置放在工作空间里，影响范围不清楚](#ux-02) |
| UX-03 | 角色与权限 | P1 | [能看页面与能操作没有统一处理](#ux-03) |
| UX-04 | Agent 编辑 | P1 | [屏幕上的修改与实际发布内容可能不一致](#ux-04) |
| UX-05 | Agent 编辑 | P2 | [版本差异是原始结构，难判断改了什么](#ux-05) |
| UX-06 | 创建与首次使用 | P2 | [示例入口和引导只在没有 Agent 时出现](#ux-06) |
| UX-07 | Agent 能力 | P2 | [能力配置与能力来源之间缺少衔接](#ux-07) |
| UX-08 | 任务列表 | P1 | [任务靠长编号识别，日常查找困难](#ux-08) |
| UX-09 | 任务状态 | P2 | [已完成任务仍显示排队信息](#ux-09) |
| UX-10 | 任务详情与调试 | P1 | [结果、消息和工具过程没有阅读层次](#ux-10) |
| UX-11 | 文件交付 | P1 | [任务完成后，用户仍拿不到生成文件](#ux-11) |
| UX-12 | 用量可信度 | P1 | [执行过程与工具次数不一致](#ux-12) |
| UX-13 | 接入与发布 | P1 | [渠道页面没有先区分使用场景](#ux-13) |
| UX-14 | 接入流程 | P1 | [完成接入需要跨页和反复保存](#ux-14) |
| UX-15 | 凭据使用 | P1 | [凭据用途和填写方式不统一](#ux-15) |
| UX-16 | 待办 | P1 | [默认列表没有以当前处理人为中心](#ux-16) |
| UX-17 | 记忆与历史 | P2 | [待办里混入搜索和主动创建操作](#ux-17) |
| UX-18 | 审批决策 | P1 | [批准之前缺少可读的操作解释](#ux-18) |
| UX-19 | 工具与技能 | P1 | [资源目录被新增表单占据](#ux-19) |
| UX-20 | 技能导入 | P1 | [上传提示与控件行为不一致](#ux-20) |
| UX-21 | 工具配置与版本 | P2 | [缺少从登记到可用的验证和影响说明](#ux-21) |
| UX-22 | 记录 | P2 | [审计、费用和用户数据属于不同工作目的](#ux-22) |
| UX-23 | 操作日志 | P1 | [筛选和结果要求用户知道内部名称](#ux-23) |
| UX-24 | 用量与错误反馈 | P1 | [累计数据不足以回答日常问题，失败也可能像空数据](#ux-24) |
| UX-25 | 用户数据 | P2 | [查找入口要求先知道外部标识](#ux-25) |
| UX-26 | 账号与接入设置 | P2 | [几个“身份/密钥”入口的用途难区分](#ux-26) |
| UX-27 | 高影响操作 | P1 | [停用登录入口的反馈与其他停用操作不一致](#ux-27) |
| UX-28 | 聊天入口 | P2 | [几种不同情况都可能显示等待接入](#ux-28) |
| UX-29 | 聊天记录与退出 | P1 | [删除、退出的名称大于实际行为](#ux-29) |
| UX-30 | 跨页视觉与操作规范 | P2 | [组件组合缺少一致的层级和操作位置](#ux-30) |

<a id="ux-01"></a>

### UX-01 · 页面按模块拼接，缺少任务主线

**全局导航 · P1 · 实测＋代码**

- 现状与影响：待办空数据有 5 个空状态，内容高 2651px；工具页空数据仍有 3 个常驻表单，高 3432px（视口 1088×1038）。合并导航只是滚动到段落，所有内容仍一起展开。 当前空间名称是静态展示，切换需返回工作空间列表。
- 建议调整：保留少量一级入口，内部用真正切换内容的二级导航。每页优先呈现当前对象、状态和主要操作；无数据只用一个紧凑空状态。 工作空间切换器放在固定导航位置；先提供简洁的运行概况与待处理入口，不堆装饰性图表。
- 验收标准：零条待办一屏内读完；从任意页两次点击内到达目标列表；历史数据不挤占待处理区域。
- 涉及工作：前端结构。
- 定位：[GroupedPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/layout/GroupedPage.tsx)；[navigation.ts](D:/projects/products/tiny-hermes/apps/web/src/layout/navigation.ts)；[styles.css](D:/projects/products/tiny-hermes/apps/web/src/styles.css)。

<a id="ux-02"></a>

### UX-02 · 平台配置放在工作空间里，影响范围不清楚

**设置 · P1 · 实测＋代码**

- 现状与影响：设置把成员、工作空间凭据、全平台模型、出站批准、服务账号和平台登录配置放在一页。页面始终展示当前工作空间，易让人误判修改只影响这里。
- 建议调整：拆出平台管理入口，清楚标识全平台与当前工作空间；普通成员只看到可用模型摘要，平台管理员在独立位置维护。提供就地跳转和返回原任务。
- 验收标准：每项配置可在操作前看出影响范围；切换工作空间不会让全平台配置看起来像不同副本。
- 涉及工作：前端＋权限核对。
- 定位：[SettingsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SettingsPage.tsx)；[ModelEndpointsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ModelEndpointsPage.tsx)；[IdentityProvidersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/IdentityProvidersPage.tsx)。

<a id="ux-03"></a>

### UX-03 · 能看页面与能操作没有统一处理

**角色与权限 · P1 · 代码核对**

- 现状与影响：工作空间列表一直显示新建按钮，但后端只允许平台管理员创建；成员页读者可见，但邀请和改角色控件没有按写权限收窄；审批卡片统一绘制批准按钮。不能把这些判断为后端越权。
- 建议调整：统一根据当前用户的操作权限显示按钮。只读页面保留可理解的信息；等待他人处理时明确负责人，不提供注定失败的操作入口。
- 验收标准：分别以平台管理员、空间管理员、开发者、只读成员和终端用户验收；每个可点击操作都与服务端授权一致。
- 涉及工作：前后端权限对齐。
- 定位：[WorkspacesPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/WorkspacesPage.tsx)；[MembersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/MembersPage.tsx)；[ApprovalsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ApprovalsPage.tsx)；[workspace_service.py](D:/projects/products/tiny-hermes/packages/backend/src/tiny_hermes/tenancy/application/workspace_service.py)。

<a id="ux-04"></a>

### UX-04 · 屏幕上的修改与实际发布内容可能不一致

**Agent 编辑 · P1 · 代码核对**

- 现状与影响：差异预览使用当前表单，保存草稿另发请求，发布只提交已保存草稿的修订号。确认框只说发布修订号，没有提示表单尚有未保存修改。本轮没有实际发布验证。
- 建议调整：明确区分未保存、已保存未发布、已发布三种状态。提供“保存并发布”：保存成功后重新生成发布预览；失败时保留输入。离开有修改的表单时给保留或放弃选项。
- 验收标准：改一处内容后发布，预览和发布版本一致；保存失败不得继续发布旧版；离开再回来不会无提示丢失修改。
- 涉及工作：前端流程＋回归测试。
- 定位：[AgentDetailPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AgentDetailPage.tsx)。

<a id="ux-05"></a>

### UX-05 · 版本差异是原始结构，难判断改了什么

**Agent 编辑 · P2 · 实测＋代码**

- 现状与影响：真实示例页把 model 对象作为 JSON 展示，包含 null 与省略字段的差异，顶部还显示长哈希。用户难以判断这是行为变化还是存储格式差别。
- 建议调整：用字段名称和前后值展示模型、指令、工具、预算、发布范围变化；仅在确认语义等价后忽略格式差异。原始结构和哈希放到技术详情。
- 验收标准：可直接读出“模型未变、输出参数改变”；技术详情仍可核对完整版本；语义变化不能被折叠规则吞掉。
- 涉及工作：前端＋语义核对。
- 定位：[AgentDetailPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AgentDetailPage.tsx)。

<a id="ux-06"></a>

### UX-06 · 示例入口和引导只在没有 Agent 时出现

**创建与首次使用 · P2 · 代码核对**

- 现状与影响：AgentsPage 把三步引导和创建示例整个放在空列表分支；有第一个 Agent 后就只能手动新建。模型选择列表也只表达已登记/启用，不能代表已完成真实运行准备。
- 建议调整：新建 Agent 始终提供从示例开始与空白创建；首次运行前检查已发布模型、工具和运行环境，缺什么给对应入口。允许已有用户重新打开引导。
- 验收标准：创建第二个示例不需先删除已有 Agent；不把模型已配置等同于已通过真实运行检查。
- 涉及工作：前端；完整检查需后端。
- 定位：[AgentsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AgentsPage.tsx)；[2026-09-06-real-model-example.md](D:/projects/products/tiny-hermes/docs/superpowers/verification/2026-09-06-real-model-example.md)。

<a id="ux-07"></a>

### UX-07 · 能力配置与能力来源之间缺少衔接

**Agent 能力 · P2 · 代码核对**

- 现状与影响：Agent 能力区有内置工具、技能版本、HTTP 操作、MCP 工具、写入策略和网络范围多个选择器；列表为空时主要显示空提示。登记工具后仍需回这里绑定并重新发布。
- 建议调整：把能力配置改为带用途说明的选择列表；空列表给“去添加”并保留草稿。选中外部操作时同步展示所需凭据、网络和审批条件。
- 验收标准：能从 Agent 编辑完成“找能力→添加→绑定→发布”，不丢当前修改；展示已绑定的确切版本和操作。
- 涉及工作：前端＋依赖查询。
- 定位：[AgentDetailPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AgentDetailPage.tsx)；[ToolingPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ToolingPage.tsx)。

<a id="ux-08"></a>

### UX-08 · 任务靠长编号识别，日常查找困难

**任务列表 · P1 · 实测＋代码**

- 现状与影响：主列为完整 UUID，没有 Agent 名或输入摘要；当前列表没有状态、Agent、时间筛选和分页。只有一次任务的环境不能证明数据多时的实际性能。
- 建议调整：主列改为任务摘要＋Agent，编号可复制但降为辅助信息；提供状态、时间和 Agent 筛选，列表展示最近活动和需要处理的原因。
- 验收标准：不记编号也能找到某个 Agent 昨天失败的任务；筛选和总量覆盖整个数据集；数据多时使用真实分页。
- 涉及工作：前端＋查询接口。
- 定位：[RunsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/RunsPage.tsx)。

<a id="ux-09"></a>

### UX-09 · 已完成任务仍显示排队信息

**任务状态 · P2 · 实测＋代码**

- 现状与影响：真实完成任务的队列列显示 terminal 和“排队第 0 位”。渲染只排除了 head，终态也进入排队文案。
- 建议调整：只对实际排队任务显示位置；完成、取消、失败任务显示对应结果和结束时间。状态主名称用中文，原始状态值放技术详情。
- 验收标准：终态没有排队位置；等待审批、等待外部响应、排队三种情况可区分。
- 涉及工作：前端。
- 定位：[RunsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/RunsPage.tsx)；[PlaygroundPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/PlaygroundPage.tsx)。

<a id="ux-10"></a>

### UX-10 · 结果、消息和工具过程没有阅读层次

**任务详情与调试 · P1 · 实测＋记录**

- 现状与影响：详情页按概要、消息、工具、产物、时间线长页展开；同一调用在消息和工具中重复。四次成功写文件没有正文，消息只显示返回箭头；tool 身份也没直接标明对应调用。
- 建议调整：默认先展示最终答复、文件和当前需处理事项；执行过程按一次调用合并输入、状态和返回。空输出明确显示“执行成功，无返回正文”，错误单独表达。原始事件按需展开。
- 验收标准：用户先看到结果；任何工具返回均可找到对应调用；失败、成功无输出、仍执行中三种状态不混淆。
- 涉及工作：前端；状态字段需核对。
- 定位：[RunDetailPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/RunDetailPage.tsx)；[PlaygroundPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/PlaygroundPage.tsx)；[transcript.ts](D:/projects/products/tiny-hermes/apps/web/src/runs/transcript.ts)。

<a id="ux-11"></a>

### UX-11 · 任务完成后，用户仍拿不到生成文件

**文件交付 · P1 · 同日真实运行记录＋代码**

- 现状与影响：真实示例的 summary.md 已持久保存，但产物列表为 0，需要后台读取才拿到文件。聊天页固定向 Transcript 传入空 artifacts，下载入口未打通。不是文字改名能解决的问题。
- 建议调整：区分工作目录文件与可交付文件，建立明确的文件发布与下载流程。完成页展示实际可下载的文件；文件未准备好时说明状态，不能凭模型回复生成虚假下载按钮。
- 验收标准：示例完成后，授权用户可直接下载正确 summary.md；越权不可下载；未交付文件不能显示成已交付。
- 涉及工作：前后端＋存储交付。
- 定位：[2026-09-06-real-model-example.md](D:/projects/products/tiny-hermes/docs/superpowers/verification/2026-09-06-real-model-example.md)；[ChatPage.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/pages/ChatPage.tsx)。

<a id="ux-12"></a>

### UX-12 · 执行过程与工具次数不一致

**用量可信度 · P1 · 同日真实运行记录＋实测**

- 现状与影响：同一真实任务记录了 7 次文件工具调用，用量页显示工具调用 0。原因尚未诊断，也不能据此断定预算限制一定失效。
- 建议调整：先查清统计口径和计数链路，统一列表、详情、汇总的含义；真实值修好前不通过改文案或隐藏数字掩盖差异。
- 验收标准：同一次任务的工具事件与统计可按明确口径核对，重试和失败也有一致规则。
- 涉及工作：后端诊断＋前端口径。
- 定位：[2026-09-06-real-model-example.md](D:/projects/products/tiny-hermes/docs/superpowers/verification/2026-09-06-real-model-example.md)；[UsagePage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/UsagePage.tsx)。

<a id="ux-13"></a>

### UX-13 · 渠道页面没有先区分使用场景

**接入与发布 · P1 · 实测＋代码**

- 现状与影响：“绑定渠道”表单没有让用户选择或明确说明渠道，提交固定为飞书；同页“注册签发方”提交固定为 web。用户需要从专业字段猜这是飞书机器人还是网页聊天。
- 建议调整：入口明确为飞书机器人、网页聊天等实际支持的场景；分别展示需要的配置和流程。未支持的渠道不画成可用入口。
- 验收标准：用户在填写密钥前已知道配置哪个入口、服务哪个 Agent、谁可以访问。
- 涉及工作：前端信息架构。
- 定位：[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)。

<a id="ux-14"></a>

### UX-14 · 完成接入需要跨页和反复保存

**接入流程 · P1 · 代码核对＋表单实测**

- 现状与影响：新建不提供长连接切换；需先保存应用凭据，重开编辑再设置接入方式，之后还可能需要重启调度服务。页面缺少贯穿这些步骤的完成状态。
- 建议调整：按选择 Agent、填写渠道配置、选择接入方式、验证消息收发引导。一次收集完整配置；后台未就绪时保留“待生效”和具体步骤。
- 验收标准：无需凭记忆重开表单补字段；已保存、已连接、已验证能收发分别显示，不能相互代替。
- 涉及工作：前后端接入流程。
- 定位：[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)。

<a id="ux-15"></a>

### UX-15 · 凭据用途和填写方式不统一

**凭据使用 · P1 · 实测＋代码**

- 现状与影响：渠道“加密密钥”下拉框出现 deepseek-api-key；模型页可选择并新建凭据，HTTP/MCP 则手填环境变量名或 ID，登录配置又允许环境变量名或凭据名称。不同字段实际约定不同。
- 建议调整：统一“选择已保存凭据”体验，显示范围和用途；按接口真实支持的引用格式转换，环境变量作为高级选项。渠道加密密钥与模型 API Key 明确区分用途并给格式检查。
- 验收标准：不用复制 UUID 或猜名称格式；不把任意已保存密钥当成渠道已配置完成；敏感值始终掩码。
- 涉及工作：前后端引用约定＋前端。
- 定位：[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)；[ModelEndpointsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ModelEndpointsPage.tsx)；[HttpToolsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/HttpToolsPage.tsx)；[McpServersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/McpServersPage.tsx)；[IdentityProvidersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/IdentityProvidersPage.tsx)。

<a id="ux-16"></a>

### UX-16 · 默认列表没有以当前处理人为中心

**待办 · P1 · 实测＋代码**

- 现状与影响：用户确认、管理员审批、历史都并列展示；侧栏数量把三个队列相加，任一个不可读就不显示数量，且不是按当前用户可处理事项计数。完整角色场景本轮未登录实测。
- 建议调整：默认“待我处理”，另设“等待他人”“处理历史”；类型作为筛选。计数与当前用户可操作列表同口径，并支持未知/更多状态。
- 验收标准：只读成员不被提示处理无权批准的事项；管理员不能替终端用户确认；完成一项后列表和计数一致刷新。
- 涉及工作：前后端权限与计数。
- 定位：[ApprovalsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ApprovalsPage.tsx)；[useInboxCount.ts](D:/projects/products/tiny-hermes/apps/web/src/layout/useInboxCount.ts)；[InboxPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/InboxPage.tsx)。

<a id="ux-17"></a>

### UX-17 · 待办里混入搜索和主动创建操作

**记忆与历史 · P2 · 实测＋代码**

2026-09-06 后续实测补充：搜索迁到记录后，以 `summary` 检索真实模型验收任务，结果包含模型过程文字，与正式回复一起标为 assistant。`memory/infrastructure/sql_search.py` 的 `_hit` 拼接全部 content.parts 的 text，没有按 part 类型区分。本轮记录该问题，未改搜索索引或结果接口；后续应区分可见对话与过程记录，避免仅在前端删文字后仍由过程文字命中搜索。任务证据：`57aaff7b-46af-4d4f-8e68-dbef7d49c881`。

同日后续修复：索引与结果片段均只取正式文本块；迁移 `20260906_0057` 已更新本地旧消息索引，保留原始消息用于模型回放。真实页面中，仅存在于过程文字的 `naming` 从 2 条结果变为没有结果，`笔记` 仍能找到用户消息与正式回复。此项搜索内容问题已解决；全文搜索仍不支持任意文件名片段匹配，记忆历史等其余范围仍待完成。见 [会话搜索验收](../verification/2026-09-06-visible-search.md)。

- 现状与影响：记忆审核下面放着搜索对话和写入共享记忆；技能提案查询未限定 pending，列表也会展示已决定状态。不同子模块的历史入口不一致。
- 建议调整：待办只承载等待决定的事项；搜索对话归入会话记录，主动维护共享记忆归入 Agent 的记忆管理。三种审批历史统一入口，同时保留类型差异。
- 验收标准：用户找聊天记录不必猜到记忆审核页；历史不会被当作新待办；共享记忆维护仍可找到。
- 涉及工作：前端重组＋查询调整。
- 定位：[MemoryPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/MemoryPage.tsx)；[SkillProposalsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SkillProposalsPage.tsx)；[InboxPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/InboxPage.tsx)。

<a id="ux-18"></a>

### UX-18 · 批准之前缺少可读的操作解释

**审批决策 · P1 · 代码核对**

- 现状与影响：控制台审批主要展示工具名、权限、任务 ID 和完整 JSON；终端用户 ApprovalBanner 主要展示工具名及同意/拒绝按钮，没有展示具体操作对象和参数。本轮没有真实待审批数据。
- 建议调整：先展示谁请求、准备改什么、目标对象、主要参数和可能影响；完整参数可展开核对。解释必须来自真实请求，不能让模型随意编写有误导性的摘要。
- 验收标准：用户在批准前能说明自己批准了哪次操作；修改参数需重新审批；控制台和终端用户都只展示其有权查看的信息。
- 涉及工作：前端＋最小授权信息接口。
- 定位：[ApprovalsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ApprovalsPage.tsx)；[ApprovalBanner.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/chat/ApprovalBanner.tsx)；[end_user_approval_routes.py](D:/projects/products/tiny-hermes/packages/backend/src/tiny_hermes/runs/presentation/end_user_approval_routes.py)。

<a id="ux-19"></a>

### UX-19 · 资源目录被新增表单占据

**工具与技能 · P1 · 实测＋代码**

- 现状与影响：空工具页有上传、导入、HTTP 登记和 MCP 登记等大块区域；先呈现配置输入，之后才看到已存在的资源。缺少按用途找能力、再查看详情的目录体验。
- 建议调整：保留“技能”和“外部工具”的真实区别，以列表为默认页面；新增用按钮打开流程，详情展示用途、版本、状态、绑定情况。来源和类型作为筛选。
- 验收标准：已有工具能直接找到；新增不会把其他列表推到几屏后；知道登记完成还需绑定 Agent 才生效。
- 涉及工作：前端目录与详情。
- 定位：[SkillsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SkillsPage.tsx)；[HttpToolsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/HttpToolsPage.tsx)；[McpServersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/McpServersPage.tsx)。

<a id="ux-20"></a>

### UX-20 · 上传提示与控件行为不一致

**技能导入 · P1 · 实测＋代码**

- 现状与影响：标题要求选择目录，但真实 file input 只有 multiple、没有目录选择属性；处理逻辑有相对路径支持，控件却未提供目录。选择文件立即发起上传，没有待上传预览。“从 Git 导入”实际接收 tar.gz 地址。
- 建议调整：明确支持目录还是文件集合，控件与说明保持一致；上传前显示路径清单和 SKILL.md 检查结果，再确认上传。Git 导入若只支持归档，就准确标为归档地址导入并提供示例。
- 验收标准：包含子目录和同名文件的技能可保持原结构；取消预览不创建资源；普通仓库链接不会被误认为可直接导入。
- 涉及工作：前端；新增格式需后端。
- 定位：[SkillsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SkillsPage.tsx)。

<a id="ux-21"></a>

### UX-21 · 缺少从登记到可用的验证和影响说明

**工具配置与版本 · P2 · 代码核对**

- 现状与影响：HTTP 工具要求直接粘贴 JSON；MCP 的重新读取会产生或沿用版本。页面可看版本和操作，但缺少统一的解析预览、绑定对象及升级影响入口。现有停用确认说明仍有价值。
- 建议调整：导入时先展示解析出的操作、读写性质和缺失条件；详情提供“绑定到 Agent”“哪些 Agent 正在使用”。升级预览兼容性和影响，发布后才切换绑定。
- 验收标准：能解释工具为何不可用；旧绑定不会随资源更新自动改变；测试写操作前展示影响并按权限批准。
- 涉及工作：前端＋依赖与校验接口。
- 定位：[HttpToolsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/HttpToolsPage.tsx)；[McpServersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/McpServersPage.tsx)；[SkillsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SkillsPage.tsx)。

<a id="ux-22"></a>

### UX-22 · 审计、费用和用户数据属于不同工作目的

**记录 · P2 · 实测＋代码**

- 现状与影响：记录页把审计表、用量统计和主体数据请求上下堆叠；查看本月费用需要经过审计，找某人的数据又在统计下面。
- 建议调整：拆成可独立访问的操作日志、用量与费用、用户数据；可共用二级导航。会话搜索有单独入口并可从任务跳转，不扩张为无限多一级菜单。
- 验收标准：能直接链接到对应页面并保留筛选；查费用与处理用户数据不必滚过其他大表。
- 涉及工作：前端信息架构。
- 定位：[RecordsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/RecordsPage.tsx)。

<a id="ux-23"></a>

### UX-23 · 筛选和结果要求用户知道内部名称

**操作日志 · P1 · 实测＋代码**

- 现状与影响：筛选是按动作/对象类型的自由文本；结果直接显示 run.completed、system、UUID 等。响应含 has_more，但页面没有处理它，也没有后续页入口；数据较多时可能把部分日志当作完整结果。当前已有导出和权限范围提示，应保留。本轮数据未达到翻页边界。
- 建议调整：提供动作分类、时间、对象和操作者选择；显示“某人发布了某 Agent”等可核对描述，技术字段可展开。日志关联有权查看的任务/Agent。 根据后端实际分页能力补后续页入口和更多记录提示。
- 验收标准：不用查内部代码即可找出谁在何时修改了对象；导出范围与页面一致，权限脱敏不被破坏。 has_more=true 时可继续查看，页面不能无提示截断。
- 涉及工作：前端＋名称与筛选接口。
- 定位：[AuditPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AuditPage.tsx)。

<a id="ux-24"></a>

### UX-24 · 累计数据不足以回答日常问题，失败也可能像空数据

**用量与错误反馈 · P1 · 实测＋代码**

- 现状与影响：用量页只有创建以来的累计数据；未展示查询错误分支，data 未返回时 buckets 为空，可进入“还没有用量数据”。当前记录为空的模型费用已正确显示未知，不能改成零。 审计页的查询失败也缺少独立错误分支。
- 建议调整：用量页独立提供时间范围及可支持的维度；先保留清楚的累计口径，再补查询能力。所有页统一区分加载、无数据、无权限、失败，并提供就地重试。
- 验收标准：网络失败不会显示为没有数据；未知费用不计为免费；选择时间范围后前后端数据口径一致。
- 涉及工作：前端状态＋后端统计。
- 定位：[UsagePage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/UsagePage.tsx)；[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)。

<a id="ux-25"></a>

### UX-25 · 查找入口要求先知道外部标识

**用户数据 · P2 · 实测＋代码**

- 现状与影响：主体数据请求需要填写渠道和外部标识；不能从这里按可读名称找人，且与设置里的控制台成员是两类身份。
- 建议调整：命名为用户数据，说明面向终端用户；提供授权范围内的查找方式，并能从会话跳转到对应人。导出/擦除前显示对象和数据范围，保留现有二次确认。
- 验收标准：不会把控制台成员和终端用户混淆；处理请求前可核实目标，搜索能力不得泄漏未授权用户。
- 涉及工作：前端＋受限查找接口。
- 定位：[SubjectDataPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SubjectDataPage.tsx)；[MembersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/MembersPage.tsx)。

<a id="ux-26"></a>

### UX-26 · 几个“身份/密钥”入口的用途难区分

**账号与接入设置 · P2 · 代码核对＋实测**

- 现状与影响：API 密钥页先创建服务账号，再创建权限项为内部名称的密钥；平台 OIDC 登录与网页聊天签发方在不同页面，名字都偏技术。登录页还始终显示初始化入口。
- 建议调整：入口按目的命名为程序访问、控制台登录、聊天用户登录；程序访问流程解释服务账号和权限范围，并提供用法示例。初始化仅在部署未初始化时作为主入口，已初始化给清楚状态。
- 验收标准：用户知道模型密钥、程序 API 密钥和登录配置分别用在哪里；不因进入初始化页误以为需要重新建管理员。
- 涉及工作：前端＋初始化状态查询。
- 定位：[ApiKeysPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ApiKeysPage.tsx)；[IdentityProvidersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/IdentityProvidersPage.tsx)；[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)；[LoginPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/LoginPage.tsx)；[BootstrapPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/BootstrapPage.tsx)。

<a id="ux-27"></a>

### UX-27 · 停用登录入口的反馈与其他停用操作不一致

**高影响操作 · P1 · 代码核对**

- 现状与影响：成员移除、凭据停用、工具撤回已有确认；身份提供方停用和网页签发方停用却直接调用 mutation，相关失败也没有同等级的就地提示。不能据此声称已发生用户被锁出的事故。
- 建议调整：对会影响登录或服务的操作先展示目标、影响范围和恢复方式，再确认；失败保留对象并显示错误。普通低影响编辑不增加无意义弹窗。
- 验收标准：停用前知道谁会受影响；失败不会表现为无反应；是否需防止关闭最后可用入口由产品与后端共同确认。
- 涉及工作：前端＋影响查询。
- 定位：[IdentityProvidersPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/IdentityProvidersPage.tsx)；[ChannelsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/ChannelsPage.tsx)；[SecretsPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/SecretsPage.tsx)。

<a id="ux-28"></a>

### UX-28 · 几种不同情况都可能显示等待接入

**聊天入口 · P2 · 入口实测＋代码**

- 现状与影响：直接打开聊天页显示等待接入，这是正常的企业授权入口。ChatHome 对 agents 请求的错误统一置空；无可用 Agent 也走等待提示。SettingsPage 的少于两个分支把零个也说成只有一个。
- 建议调整：保留企业登录边界，区分未授权、授权已失效、暂无可用 Agent、服务错误，并按原因给返回企业入口或重试。默认 Agent 的零、一、多三个状态分别处理。
- 验收标准：服务异常不会让用户反复从企业入口登录；零个 Agent 不显示只有一个；不新增绕过企业授权的普通登录。
- 涉及工作：前端错误分类。
- 定位：[ChatHome.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/pages/ChatHome.tsx)；[SettingsPage.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/pages/SettingsPage.tsx)；[App.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/App.tsx)。

<a id="ux-29"></a>

### UX-29 · 删除、退出的名称大于实际行为

**聊天记录与退出 · P1 · 代码核对**

- 现状与影响：会话菜单的删除只是移出本机列表；提示虽然说明了这一点，主操作名称仍是删除。设置里的退出也只调用 forgetAllSessionIds，没有调用退出或会话失效接口。
- 建议调整：按实际行为命名为从本机列表移除、清空本机记录；若保留“退出登录”，实现真实会话结束。明确本机记录与服务器数据的区别，提供可恢复的会话历史方案。
- 验收标准：清空列表不被描述成服务端删除；真正退出后旧会话凭证失效；刷新页面行为与用户看到的说明一致。
- 涉及工作：前端命名；真实退出需后端。
- 定位：[SessionItem.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/chat/SessionItem.tsx)；[localSessions.ts](D:/projects/products/tiny-hermes/apps/chat-web/src/chat/localSessions.ts)；[SettingsPage.tsx](D:/projects/products/tiny-hermes/apps/chat-web/src/pages/SettingsPage.tsx)。

<a id="ux-30"></a>

### UX-30 · 组件组合缺少一致的层级和操作位置

**跨页视觉与操作规范 · P2 · 实测＋代码**

- 现状与影响：长页大量同权重大卡片、重复空插图；普通按钮、原生文件选择、表格与原始 JSON 混用。草稿主操作分布在页面顶部和底部。间距粘连、滚动回弹和需求式文案已修复，不应再算未完成缺陷。
- 建议调整：建立页面、列表、详情、编辑、审核五类模板；统一主按钮、保存状态、错误位置和紧凑空状态。长表单用分段导航及固定操作栏；原始结构限制宽度并允许复制。
- 验收标准：同类页面可预测；主操作可通过键盘到达；长文本不撑破页面；深浅主题均能区分状态，且不只依赖颜色。
- 涉及工作：前端设计规范。
- 定位：[styles.css](D:/projects/products/tiny-hermes/apps/web/src/styles.css)；[GroupedPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/layout/GroupedPage.tsx)；[FormSection.tsx](D:/projects/products/tiny-hermes/apps/web/src/forms/FormSection.tsx)；[AgentDetailPage.tsx](D:/projects/products/tiny-hermes/apps/web/src/pages/AgentDetailPage.tsx)。

## 需专项验证的风险（不计入上述 30 项已记录事项）

| ID | 线索 | 当前能确认什么 | 下一步验证 |
| --- | --- | --- | --- |
| R-01 中文输入法 | Composer 的 Enter 处理未判断 isComposing | 代码中没有组合输入保护；未做真实中文输入法测试，不能声称每次都会误发送 | 用系统中文输入法选字回车应只确认候选，结束组合后的回车才发送；同时验证粘贴和 Shift+Enter |
| R-02 窄屏与长文本 | 记忆搜索框 minWidth=320，多个内联表单和完整 ID/JSON；统一样式未覆盖所有原始内容类 | 桌面已看页面没有出现全局横向溢出，不能证明手机也正常 | 320/390/768/1280px 检查页面和弹窗；长文件路径、工具参数、中文和英文都要覆盖 |
| R-03 完整键盘与辅助阅读 | 自定义会话菜单、折叠表单、多角色操作、主题色 | 已有 aria-label、原生按钮、状态文字和部分错误自动展开等基础，不能以此声称全面达标 | Tab 顺序、焦点返回、Esc、可见焦点、屏幕阅读器名称及实际对比度；不因仅看截图就判颜色不达标 |

定位：[Composer](D:/projects/products/tiny-hermes/apps/chat-web/src/chat/Composer.tsx)、[MemoryPage](D:/projects/products/tiny-hermes/apps/web/src/pages/MemoryPage.tsx)、[SessionItem](D:/projects/products/tiny-hermes/apps/chat-web/src/chat/SessionItem.tsx)、[FormSection](D:/projects/products/tiny-hermes/apps/web/src/forms/FormSection.tsx)。

## 推荐实施顺序

| 批次 | 重点 | 对应事项 | 完成标志 |
| --- | --- | --- | --- |
| A：先保证操作和结果可信 | 发布与保存、文件交付、计数、上传目录、退出含义、查询失败/日志截断；先验证输入法风险 | 04、11、12、20、23、24、29、R-01 | 不发布错内容，不隐藏失败，不误称已交付/已退出；数据可核对 |
| B：重排主流程 | 待办、能力库、接入发布、记录/设置范围、角色可操作入口 | 01、02、03、13—19、22 | 用可点击原型走通首次配置、接入、审批和能力绑定；确认结构后分页面落地 |
| C：提高日常使用效率 | Agent 版本差异、示例与能力选择、任务查找、结果页、工具版本、用户数据、程序访问 | 05—10、21、25、26、28 | 不懂内部编号也能找到对象并完成常用任务 |
| D：统一视觉与状态 | 页面模板、就地反馈、高影响操作、窄屏与键盘 | 27、30、R-02、R-03 | 同类页面行为一致，深浅主题与目标屏宽验收通过 |

跨批依赖：B 的角色/审批信息接口应与 A 同时确认；不能先画“待我处理”再沿用错误计数。新增搜索、分页、费用维度、使用关系、下载和真实退出，都需要核实或补充后端能力，不能只画出控件。

## 可点击原型应该先验证什么

- 待办：普通管理员打开就知道哪些需要自己处理；每条展示请求者、动作、对象和影响；历史不常驻展开。
- 能力库：默认列表和搜索；点击某能力看用途、确切版本、可用状态、绑定对象；新增流程独立。
- 接入发布：先选择飞书或网页；按场景显示所需配置；显示缺少条件与真实验证状态。
- 任务详情：顶部展示结果与交付文件；进行中的任务显示等待原因和可执行动作；工具过程按调用展开。
- Agent 编辑：固定保存/发布区域，清楚区分表单修改、已保存草稿与已发布版本；试用默认版本明确。

这些原型只用明确标注的示意数据，不能把“测试连接”“一键发布”等尚不存在的后端能力装成已完成。

## 应保留的已有设计

- 工作空间隔离、平台/空间/终端用户权限边界，用户确认不能由管理员冒名代答。
- 已发布版本固定绑定、写操作审批、删除和撤回影响说明。
- 已有凭据掩码、模型页就地存新凭据、缺模型提示、部分查询重试入口。
- 未知费用不计为零，已脱敏审计要明确提示范围，导出遵守相同授权。
- 折叠表单保留输入值并在关联校验错误时展开；这些优点应扩展到所有同类页面。
- 不将所有高级字段一概删除；改为按任务提供默认值、解释和展开入口。

## 本日已修复，不重复记为待办

需求式/重复文案已清理；待办与 Agent 详情的卡片粘连已修复并实测 20px；合并页标题多余留白已收紧；合并页已替换导致滚动回弹的锚点组件；模型价格 undefined 已修复。本报告针对修复后的页面，仍可观察到上述工作流程问题。成功无输出的工具返回目前尚未改为说明性显示，仍属于 UX-10。

## 这一遍没能证明什么

没有多角色真实账号矩阵、真实待批准事项、大量历史记录、移动端或系统输入法完整实测；聊天端未取得终端用户会话；未重新执行外部渠道收发、数据擦除、角色修改、模型发布或实际工具登记。本轮没有改产品代码，也没有因此重跑全部测试；代码证据与界面证据分列，不能拿“代码能看出”冒充“流程已跑通”。

## 不声称什么

不声称这 30 项都是后端缺陷、不声称现有产品整体不可用，也不把当前空数据当作能力不存在。真实模型示例已经跑通，但文件交付与工具统计有独立缺口。此报告不是已审批实施规格、完整无障碍认证或上线验收结论。

# tiny-hermes 产品与系统设计 v2.9.2

> 日期：2026-08-31  
> 版本：v2.9.2  
> 状态：已确认；M1 与 M2 已交付，可作为 M3 实施依据（§7.4.2 与 §12.1 已实现）  
> 目标版本：首个企业预览版  
> 许可证方向：Apache-2.0

## 1. 文档目的

本文定义 tiny-hermes 首个企业预览版的产品范围、系统边界、核心流程、UI、技术栈、安全规则和验收标准。

`tiny` 指精简、边界清晰的 Agent 内核和单机可部署形态，不表示平台只提供聊天能力，也不降低租户隔离、安全治理和运行可观察性的要求。

本文不是对 FastClaw、Hermes Agent 或 OpenClaw 的功能宣传进行改写。上游能力已经通过官方仓库、官方文档和固定版本源码核对：

- [FastClaw 与 Hermes Agent 一手资料研究](../../research/2026-08-09-fastclaw-hermes-analysis.md)
- [Hermes Agent 与 OpenClaw 的真实差异](../../research/2026-08-09-hermes-openclaw-differentiators.md)

上游事实与 tiny-hermes 的设计决定必须分开理解。上游项目存在某项功能，不代表 tiny-hermes 已经实现或可以直接复制。

### 1.1 v2.4 修订重点

v2.4 补全重试链共享预算、RunEvent 并发序号、幂等并发创建、工作空间选择、累计执行时间与墙钟期限，并把 Runs API 的“已创建但排队受阻”与 Chat Completions 的 409 `session_blocked` 明确区分。M1 的跨 Run 共享只读层缩小为平台预构建的运行时 Docker 镜像层，不在运行时根据 Agent 依赖构建快照；同时增加 cache 重置信号、沙箱所有权校验、待提交对象登记和 M1 不交付审批系统的安全边界。v2.4 替代 v2.3 中对应定义。

### 1.8 v2.9.2 修订重点

v2.9.2 只改 §12.1 一句话的措辞：「`goal_outcome` 必须记下它是被打断的」改为
「`goal` 文档必须记下它是被打断的（新增 `preempted` 字段），`goal_outcome`
不因此改写」。

起因是实现落地后代码评审的追问：`goal_outcome` 这个字段字面上仍然是判断器
真实给出的 `continue`，被打断的事实记在同一个 `goal` 对象里新增的
`preempted` 字段上，不在 `goal_outcome` 本身——这和旧措辞的字面不一致，需要
判断哪一边该让步。

结论是实现对，改的是措辞。`goal_outcome` 是判断器（`goal.judge`）对这一轮证据
给出的裁决，`decide_after_round` 是否因此结束 Run 是平台另一层的决定——两者
故意分属两个模块（`goal.py` 与 `slice_policy.py`），`decide_after_round`
自己的 docstring 早就说明「The judge answers what happened to the goal; what
happens to the Run is decided here」。把 `goal_outcome` 改写成
`preempted`（或让它凭空多出一个第六种取值）会把这两层重新粘回一起，且会丢
信息：`continue` 之外还带着 `goal_unmet`——被打断之前模型自己判定还差什么，
这条信息只有 `outcome` 仍然是 `continue` 时才留得住。旧措辞真正要防的是
「别让一个没达成目标的 Run 看起来像是`done`」——这一点 `goal_outcome` 从未
被改写成 `done`，从未失守；新措辞把「记下被打断」这件事挂在 `goal` 文档整体
上，不再误导读者以为答案必须落在 `outcome` 这一个字段里。

### 1.7 v2.9.1 修订重点

v2.9.1 收窄 §12.1 让位规则的触发条件：从「同一 Session 中已有排队的 pending Run」
改为「存在一个在当前 Run 开始之后创建的排队 pending Run」。

起因是实现时一条既有测试暴露的后果：用户连续发出的多条消息各自创建 Run 并在第一条
开始前就排好队，按原措辞它们互相触发让位，除最后一条外每条都只跑一轮。那不是插话，
是排队。

### 1.6 v2.9 修订重点

v2.9 只改 **§12.1 与 §12.3**：判为 `continue` 的自动续跑，在同一 Session 出现排队消息时
必须让位。

起因是一次真机走查里暴露的更基本的问题：一个有常驻任务的 Agent，收到任何一句话都会
回去重跑那个任务，因为它的任务被写在「人格」里、每轮当作身份注入。上游把身份（`SOUL.md`）
与常驻任务（`/goal`）分开，并用一条机制规则保证用户说话时任务让位——本平台两处都有
对应物（人格字段、§12 的 Goal 循环与 §12.2 的完成条件），缺的正是那条让位规则。

**本次只补规则，不改人格字段的约束。** 让作者不把任务写进人格，需要一个「什么算任务式
内容」的判据；写不出好判据的规则要么误伤要么形同虚设。

### 1.5 v2.8 修订重点

v2.8 改 **§7.4.2** 与 **§12.4**，补上 v2.7 留下的一个洞：摘要生成是一次真实的模型调用，
会在真实端点上花钱、占一次调用配额，但那次调用从未计入过 Run 的预算范围，也从未出现在
事件流里——一个摘要总是失败的 Session 会每轮都白付一次没人算得到的钱、白占一次没人管的
调用配额，那次调用本身也永远不可能把 Run 推到 §12.4 的任何一道上限。现在它的调用次数、
usage 与费用三者一起计入（§12.4 新增的产品决定：三个阀门是一件事一起遵守，不是选两个），
价格遵守 §12.4 原有的钉价规则而不是现读现算；只要拿到了答复就记账，不论 stop_reason 是否
completed，真正不计的只有从未拿到答复的调用；累计的数字要能在下一轮的预检里读到，并单独
记一条 RunEvent，带上端点、模型、token 数和费用。

### 1.4 v2.7 修订重点

v2.7 只改 **§7.4.2**，把上下文压缩从「装不下才动」改成「到达比例就动」，并把摘要
从确定性的结构清单换成模型生成的语义摘要——**生成一次、持久化、之后每轮读存下来的
那一份**。

起因与 v2.6 同源。v2.6 给了用户一个人工出口（`/undo`、`/new`），但没有解决使那次事故
成为可能的机制：历史里的错话没有任何处理办法。压缩是唯一会重写历史的环节，而当时的
压缩只在窗口装不下时触发，判据是尺寸不是内容——那条 80 条消息的会话离 128k 窗口差得
远，压缩一次都没跑过。

### 1.3 v2.6 修订重点

v2.6 增加 **§19.4 聊天内命令**（`/undo` 与 `/new`），并让三处跟上：§14.3 说明被收回的消息对会话搜索不可见；§20 的 Web Chat 与 §19.2 的飞书状态卡片指向 §19.4 定义的「新建会话」语义——它是在同一 Session 内划线，不是创建新的 Session 实体。

起因是一次真实事故：一条飞书会话被它自己的历史锁死（模型在图片管道故障期间说过五次「我看不到图」，管道修好后仍把那些当作已确认的事实），而产品没有给用户任何脱身的办法，唯一的出路是直接删数据库里的映射行。§916 与 §935 早已要求「新建会话」入口，但一处都没有实现。

### 1.2 v2.5 修订重点

v2.5 改两处。

**§4.5** 补全终端用户身份的四个未决问题，它们此前只写了「两个身份体系」，没写这个
体系怎么运作，而 M3 的多数条目都压在上面。四条决定是身份来源、Agent 分配、会话
可见性和聊天界面形态，见 §4.5.1 至 §4.5.4。

**§4.6** 矩阵中「本人会话与私有记忆」一行，开发者从「否」改为「查看须审计，不可
更正或删除」。理由是开发者要修的正是这个 Agent，而一段读不到的对话意味着一个查不出
的故障；代价是每次读取留痕。更正与删除仍然只属于主体本人与代办的管理员——那两个动作
改变的是主体自己的数据，不是排障需要。

v2.5 替代 v2.4 中 §4.5 与 §4.6 对应行的定义，其余部分不受影响。

## 2. 上游事实与取舍

### 2.1 FastClaw

FastClaw 更适合参考多用户控制面、上游最终用户映射、API Key 范围、管理后台和状态存储方式。

已核实的限制：

- FastClaw 官方没有声称自己是 OpenClaw 的 fork，只能确认兼容部分技能和插件约定。
- FastClaw 主要是用户所有权模型，不是完整企业组织与 RBAC 模型。
- FastClaw 使用带多租户和品牌限制的 source-available 许可证，不能按普通 Apache-2.0 项目直接复制或派生。

因此，tiny-hermes 只借鉴思想和公开接口形态，不复制 FastClaw 前端或受其附加条款约束的源码。

### 2.2 Hermes Agent

Hermes Agent 更适合参考 Agent 内核、自动 Goal 循环、REST Runs API、技能渐进加载、上下文压缩和训练轨迹数据结构。

Hermes 的主要可借鉴亮点包括：

- 同一个 Agent 内核服务 API、消息渠道、批处理和后台任务。
- Goal 在每轮后判断 `done / continue / wait`，可以自动继续或等待外部条件。
- Runs API 明确支持创建、状态、事件、停止和审批。
- 技能和记忆能够由 Agent 提议创建或修改，并可接入审批。
- 上下文压缩对工具结果、最近消息和结构化摘要有明确处理流程。
- 运行轨迹可以转换为训练格式。

Hermes profile 不是企业租户边界，也不是安全沙箱。tiny-hermes 必须自行建设工作空间、权限、审计和强制沙箱。

### 2.3 OpenClaw

OpenClaw 与 Hermes 共同拥有子 Agent、记忆、技能自学习、长任务、审批和上下文压缩。不能把这些能力简单称为 Hermes 独有。

OpenClaw 更值得借鉴的治理能力包括：

- 子任务结果交付、重试和保留。
- 记忆来源、分层和污染防护。
- 技能提案、扫描、哈希绑定、审批和回滚。
- 更明确的 Docker 默认加固。
- 审批绑定实际工具参数，参数变化后批准失效。

tiny-hermes 的组合方向是：Hermes 的自主执行体验，加上 OpenClaw 风格的生产治理和沙箱加固。

## 3. 产品定位

tiny-hermes 是面向企业私有部署的开源多 Agent 运行与编排平台。

企业可以在自己的环境中完成以下完整链路：

> 创建 Agent → 配置人格与能力 → 测试 → 发布 → 通过 Web、飞书或 API 使用 → 查看运行、费用和审计 → 暂停或恢复异常任务 → 审批记忆与技能改进

产品由三部分组成：

1. 精简的 Hermes 风格 Agent 内核。
2. 面向企业的多工作空间控制面。
3. 强制隔离的不可信工具执行面。

产品不等同于“给 Hermes 加一个后台”，也不等同于“复刻 FastClaw”。

## 4. 首版目标用户

### 4.1 平台管理员

负责安装平台、配置模型、OIDC、安全策略、全局安全阀和平台级审计。

### 4.2 工作空间管理员

负责成员、凭证、技能、工具、Agent、渠道和工作空间策略。

### 4.3 Agent 开发者

通过仪表盘或配置包创建、测试、发布、回滚和观察 Agent。

### 4.4 查看者

可以查看允许范围内的 Agent、Run、用量和审计信息，不能修改生产配置。

### 4.5 终端用户

通过 Web、飞书或企业自有应用调用 Agent，不进入管理后台。

终端用户与平台成员是两个不同身份体系。飞书用户不会自动成为工作空间成员。

贯穿以下四条的是同一句话：**平台不认识终端用户，是企业认识他。** 身份由企业担保，
授权由企业分配，界面嵌在企业自己的页面里；平台只保存一个映射和一段对话。

#### 4.5.1 身份来源：企业担保，平台只做映射

终端用户**不向 tiny-hermes 认证**。他在企业自有系统里已经登录，企业为他签发一张
短期凭证，平台验签后把它映射成 `ExternalIdentity`（§278），唯一键仍是
`workspace_id + channel + external_user_id`。

- **Web 渠道**：`external_user_id` 是企业凭证里的主体标识。工作空间登记签发方与公钥。
- **飞书渠道**：`external_user_id` 是飞书用户 ID，签发方是飞书。

两个渠道走同一条路径与同一张表，`ExternalIdentity` 因此只做一件事。跨渠道合并身份
仍必须显式绑定（§282）。

平台**不**为终端用户提供密码、邮件链接或找回流程，也不存储终端用户的邮箱等可识别
信息，除非企业主动在凭证中提供。这是 §344 清除流程能够保持廉价的前提。

演示与自测由工作空间管理员在控制台签发测试凭证，走同一条验签路径，平台不因此成为
身份提供方。

#### 4.5.2 Agent 分配：两层，都要通过

终端用户可以调用某个 Agent，必须同时满足：

1. **平台侧闸门**：该 AgentVersion 声明允许终端用户调用，由工作空间管理员决定。
2. **企业侧分配**：企业签发的凭证中列明该终端用户可用的 Agent。

平台校验凭证中列明的 Agent 确实属于该工作空间，企业不能借签发扩大范围。这与 §16.2
的两次权限检查同构：一次决定「可以被调用吗」，一次决定「这个人可以调用吗」。

分配关系**不存储在平台**。企业的人员入职、离职与调岗不需要同步给平台，这是 §4.5.1
省下的成本，不应在此处退回。

#### 4.5.3 会话可见性：状态与内容分开

终端用户的会话内容属于终端用户。工作空间成员的可见性按 §4.6 矩阵，并作如下区分：

| | 状态、轮次、错误、工具调用、耗时与费用 | 消息正文与私有记忆 |
|---|---|---|
| 工作空间管理员 | 可见 | 代办并审计（§4.6） |
| 开发者 | 可见 | **可见，但每次读取写审计** |
| 查看者 | 可见 | **不可见** |

开发者可见，是因为他要修的正是这个 Agent，而一段读不到的对话意味着一个查不出的
故障。**代价是留痕**：每一次读取写下谁在什么时候读了谁的对话，企业问「谁看过」时
答得上来。查看者不可见——查看者不修东西，没有读的理由。

开发者可以**看**，不能**更正或删除**。那两个动作会改变主体自己的数据，仍然只属于
主体本人与代办的管理员（§4.6）。

状态与内容的区分本身不是新规则：§4.6 在 Run 那一行已经把「控制」与「可查看状态」
分开，此处只是声明它同样适用于终端用户会话。

#### 4.5.4 聊天界面：独立应用

终端用户不进入管理后台（§4.5 首句），所以聊天界面不能是控制台的一条路由。平台交付一个
独立 Web 应用，企业以 iframe 嵌入或新窗口打开，携带 §4.5.1 的凭证进入。

控制台仍是控制台（§928）。可嵌入 widget 与 SDK 不属于首版；它们是同一个应用的封装，
以后增加不会浪费现在的工作。

### 4.6 固定角色权限矩阵

`管理` 表示可以创建、修改和删除；`控制` 表示可以启动、暂停、继续和取消；`本人` 表示只能操作自己的数据或自己发起的请求。

| 资源与动作 | 平台管理员 | 工作空间管理员 | 开发者 | 查看者 | 终端用户 |
|---|---:|---:|---:|---:|---:|
| 实例设置与工作空间创建 | 管理 | 否 | 否 | 否 | 否 |
| 工作空间成员与角色 | 管理 | 管理 | 否 | 只读 | 否 |
| 密钥、安全策略与渠道 | 管理元数据，不查看明文 | 管理元数据，不查看明文 | 使用已授权绑定 | 否 | 否 |
| Agent 草稿 | 管理 | 管理 | 管理 | 只读 | 否 |
| Agent 发布与回滚 | 管理 | 管理 | 管理 | 否 | 否 |
| Run 启动 | 控制 | 控制 | 控制 | 否 | 已分配 Agent |
| Run 暂停、继续与取消 | 控制 | 控制 | 控制自己或获授权的 Run | 否，可查看状态 | 本人 |
| 用户确认审批 | 否，不可代批 | 否，不可代批 | 否，不可代批 | 否 | 仅发起人本人 |
| 工作空间治理审批 | 是 | 是 | 否 | 否 | 否 |
| 本人会话与私有记忆的查看、更正、删除和导出 | 依法代办并审计 | 本空间代办并审计 | 查看须审计，不可更正或删除 | 否 | 本人 |
| 审计记录 | 跨空间只读并留痕 | 本空间只读 | 与自己资源相关的只读 | 脱敏只读 | 否 |

API Key 和服务账号权限是表中主体权限与 Key scope 的交集，不能通过签发 Key 扩大主体权限。平台管理员跨工作空间操作必须写入审计记录。

## 5. 已确认的产品决定

1. 首版面向企业私有部署，不建设公有 SaaS 注册、计费和套餐。
2. 一个部署实例服务一家企业，工作空间是主要租户边界。
3. 首版交付 Web、飞书和统一 API；其他企业 IM 只提供适配接口。
4. 对话任务和自主后台任务同等重要，底层共用 Run 模型。
5. Agent 默认自主运行到判断完成；平台管理员仍能设置时间、步骤和费用安全阀。
6. Agent 采用“可视化 + 配置即代码”的双向创建方式。
7. 记忆包含 Agent 共享记忆和终端用户私有记忆。
8. 工具支持内置工具、MCP 和 OpenAPI/HTTP，不允许首版上传任意本地插件。
9. 模型支持 OpenAI 兼容接口、Anthropic 和 Gemini，其他提供方通过适配器扩展。
10. 首个交付目标是 Docker Compose，不承诺首版 Kubernetes 和多副本高可用。
11. 登录支持本地账号和 OIDC，权限采用固定角色。
12. 项目采用 Apache-2.0 许可证方向，发布前进行许可证审计。
13. 实施采用纵向切片，先打通创建、发布、运行和治理闭环。
14. 技术栈采用 Python 模块化后端、独立 Worker 和 React 前端。

## 6. 首版明确不做

以下能力不是永久取消，而是不属于首个企业预览版：

- 公有 SaaS 注册、订阅、账单、发票和商业套餐。
- Kubernetes、Helm、多副本、高可用和跨区域部署承诺。
- 企业微信、钉钉及大量海外消息渠道的内置实现。
- 两层以上递归子 Agent、自由组队、投票和长期 Agent 社会。
- 跨工作空间的 Agent 委派和共享记忆。
- 公共技能市场的账号、排名、评论和付费体系。
- Agent 静默安装未知技能或直接修改已发布技能。
- 任意本地插件代码上传。
- 完整知识库、知识图谱和复杂 RAG 产品。
- 浏览器、桌面、语音和图像自动化工具。
- 多种云沙箱、SSH、HPC 和远程 Node。
- 完整训练平台、自动评估和数据集发布。
- 精确恢复到进程崩溃前的任意代码指令。

## 7. 系统结构

### 7.1 使用入口

- 管理仪表盘。
- 终端用户 Web Chat。
- 飞书适配器。
- 运行 API 和管理 API。

所有入口调用同一个 Run 服务和 Agent 内核，不复制各自的运行逻辑。

### 7.2 企业控制面

负责：

- 工作空间、成员和权限。
- Agent 草稿、发布和回滚。
- 模型、技能、工具和渠道配置。
- 密钥、安全策略和安全阀。
- 用量、审计和审批。

控制面决定“谁可以做什么”，不直接执行不可信代码。

### 7.3 Run 编排层

负责：

- Run 持久化状态和事件。
- 自动 Goal 连续执行。
- 一层父子 Agent 委派。
- 暂停、恢复、取消和审批。
- 时间、步骤、费用和子 Agent 上限。
- 子任务结果可靠交付。

独立 `scheduler` 进程负责租约回收、等待和审批超时、兼容调用清理、幂等记录过期以及保留期任务。Worker 不兼任这些全局扫描工作。

### 7.4 轻量 Hermes Agent 内核

内核保持小而明确，只包含：

- `PromptBuilder`：按固定顺序拼装人格、技能摘要、记忆、上下文和请求。
- `ModelProvider`：统一模型调用、流式输出和备用模型。
- `ToolRegistry`：列出本轮实际可用的工具。
- `MemoryStore`：读取和提出记忆变更。
- `SkillLoader`：渐进加载已发布技能版本。
- `ContextManager`：裁剪工具输出、生成结构化摘要和保留原始引用。
- `AgentLoop`：执行模型与工具循环。

#### 7.4.1 Provider 中立消息格式

Agent 内核只读写 `CanonicalMessage`，不直接持久化 OpenAI、Anthropic 或 Gemini 的消息对象。CanonicalMessage 包含 `role` 和有序 ContentBlock；首版 ContentBlock 类型为文本、图片、附件引用、工具调用和工具结果。工具调用使用平台生成的稳定 ID、工具名和 JSON 参数，工具结果必须引用对应调用 ID。

Provider Adapter 负责系统消息位置、并行工具调用、流式分片、停止原因和 usage 的双向转换。Provider 原始响应只作为受保留期和脱敏策略约束的诊断资料，不能成为跨 Provider 重放的唯一数据。

备用模型切换规则：

- 当前轮尚未持久化任何助手输出或工具调用时，可以把同一 CanonicalMessage 转换后交给备用 Provider 重试。
- 已向用户发送部分输出、持久化工具调用或发生外部副作用后，不能在同一轮静默切换 Provider；Run 进入 `interrupted` 或 `paused(operator)`，由新的安全步骤继续。
- Provider 特有内容无法转换时明确返回 `provider_history_incompatible`，不能猜测或丢弃后继续。
- 平台不要求、保存或转发 Provider 的隐藏推理内容。

#### 7.4.2 上下文预算与裁剪顺序

每个 `ModelEndpoint` 明确声明输入窗口、最大输出和 `context_accounting`：`shared` 表示输入与预留输出共用同一窗口，`separate` 表示两者分别受端点声明的上限约束。Adapter 必须按端点能力计算，不根据 Provider 名称猜测。

预算使用绝对 Token 与窗口总上限双重约束，不使用单纯百分比。每段配置 `min_tokens`、`target_tokens`、`max_tokens`、是否可裁剪和裁剪优先级；未用完的目标预算可以交给最近历史。下表是实例默认配置，不是不可改的产品上限：

| 内容段 | `min_tokens` | `target_tokens` | `max_tokens` | 裁剪规则 |
|---|---:|---:|---:|---|
| 服务端安全规则 | 512 | 1,024 | 2,048 | 不可裁剪 |
| 人格 | 256 | 1,024 | 2,048 | 不可静默裁剪 |
| 技能摘要 | 0 | 768 | 1,536 | 先移除未命中技能 |
| 记忆 | 0 | 1,536 | 3,072 | 先移除低相关记忆 |
| 工具 schema | 0 | 4,096 | 12,288 | 只能按完整工具缩减，不能截断 schema |
| 旧工具结果 | 0 | 1,024 | 2,048 | 最先裁剪，保留原始引用 |
| 最近历史 | 0 | 剩余空间 | 剩余空间 | 最后做结构化压缩 |

平台管理员在实例设置中配置默认值和硬上限；工作空间管理员和 Agent 开发者可以在实例硬上限和 ModelEndpoint 实际窗口内，为 AgentVersion 调整各段预算。当配置的目标值无法装入端点窗口、但不可裁剪的最小内容仍可装入时，发布 API 返回 `context_budget_unsatisfied` 和逐段缩放建议；建议不会静默生效，必须由开发者明确接受或修改后再发布。

`min_tokens` 是最低保留预算，不表示不足时填充无用内容。当前用户请求必须完整保留；如果当前请求、服务端安全规则、人格和输出预留等不可裁剪内容的最小合计已超过端点预算，静态配置在发布时失败，运行时输入则进入 `paused(context_overflow)`。

裁剪顺序固定为：旧工具大结果 → 未命中的技能摘要 → 低相关记忆 → 旧会话的结构化压缩。工具调用与工具结果不能拆开。Agent 发布时若固定规则、人格或静态工具 schema 超过各自硬上限，配置校验失败。

压缩结果必须记录覆盖的消息范围和原始引用。

**主动裁剪：不花钱的那几级不等压缩线。** 上面那张表把「旧工具结果」的裁剪和「旧会话的
结构化压缩」放在同一个顺序里，实现因此把它们绑在同一个触发点上——不到
`compaction_threshold` 一级都不动。**这在大窗口模型上是错的**：1M 窗口、阈值 0.50，
意味着要攒到 500K 才开始清理，而那之前每一轮都在把早已无用的工具输出逐字重发一遍。

所以旧工具结果有**自己的触发点**，与 `compaction_threshold` 无关：

- **触发用绝对 Token 数，不用比例**（`PROACTIVE_PRUNE_TOKENS`）。比例会随窗口放大，
  正是要避开的那件事。
- **保护最近内容按消息条数，不按 Token**（`PRUNE_PROTECTED_RECENT_MESSAGES`）。按
  Token 保护会从压缩阈值推导出一个巨大的尾部（1M 窗口上约 100K），把整个会话都护住，
  于是什么也裁不掉。
- 三遍都是确定性的，**都不调模型**：①去重字节完全相同的工具结果，保留最新的完整副本，
  更早的完全重复改成回指；②把尾部之外、超过 `PRUNE_MIN_RESULT_CHARS` 的工具结果打成
  存根；③截断尾部之外过大的工具调用参数。第①遍无损，因此不受尾部保护限制。

**缓存契约。** 一次裁剪会改写模型已经见过的消息，从最早被改写的那一条起，provider 的
前缀缓存全部失效——和一次压缩边界一样。因此：

- 只有当三遍合计回收 ≥ `PRUNE_MIN_RECLAIM_TOKENS` 时才采纳，否则原样返回，一个字不改。
- 触发之后**一次裁完所有够格的内容**，不是每轮啃一点。稳态下每轮新增的改写只发生在
  尾部边界那一条，失效范围因此有上界，而不是每轮从很早的历史开始失效。

**这一节偏离了本文件上面那张表所隐含的读法**（四级共用一个触发点），理由是上面写的
大窗口失效；参照实现是 `NousResearch/hermes-agent` @ `3f83297` 的
`agent/context_compressor.py::prune_tool_results_only`，它的 docstring 描述的正是同一个
失效场景与同一套闸门。段预算表里「旧工具结果 `max_tokens` 2,048」**不作为这条路径的
判据**：按它每轮裁到 2K 等于每轮改写历史、每轮打断缓存，收益被缓存损失吃掉。

**压缩由比例触发，不等到装不下。** 每轮估算的输入达到端点窗口的 `compaction_threshold`
（实例默认 0.50）即触发压缩；窗口装不下时的处理是保留的兜底，不是唯一入口。阈值与各段
预算同权限：平台管理员配置默认值和硬上限，Agent 开发者在硬上限内为 AgentVersion 调整。

以尺寸为唯一判据的压缩解决不了历史里的错话，这一点必须写明，避免把压缩当成纠错机制：
阈值下调只是让重写历史的机会更早到来，不会让任何一段历史变得更准确。

**旧会话的压缩产出模型生成的结构化摘要。** 摘要至少覆盖：目标、约束与偏好、进展、已作出
的决定、涉及的对象、下一步、关键事实。后续压缩把上一份摘要交给模型更新，而不是从头重
写，过时的条目在更新中移除。

**摘要生成一次并持久化，之后每轮读存下来的那一份。** 这是硬性要求而非实现选择：预算规划
是纯函数且每轮重算，若摘要在其中即时生成，同一个 Run 重放会得到不同的上下文，每轮还要
多付一次模型调用。摘要文本与它覆盖的消息范围一同持久化；重放读存下来的那一份，不重新
生成。

**失败按阶梯降级，每一级都不静默丢内容：** 模型生成失败（超时、拒绝、窗口不足）→ 退回
确定性的结构摘要（覆盖范围、消息条数与角色分布、调用过的工具、可搜索的线索词）；结构
摘要仍装不下 → 保留原文；原文也装不下 → `paused(context_overflow)`，不继续请求 Provider，
也不静默删除中间消息。**任何一级都不得以「摘要失败」为由丢弃中间消息。**

**摘要模型的窗口不得小于主模型的窗口**，且必须在配置校验时拒绝，不能等到压缩时才失败。
默认使用该 AgentVersion 自己的模型端点——同一端点自动满足这条约束；显式指定其它端点时
由发布校验把关，与 `context_budget_unsatisfied` 同一条路径。

**压缩事件必须记录**摘要由哪个端点、哪个模型生成，以及它是模型摘要还是降级后的结构摘要。
运维看到一段被压缩过的会话时，必须能分辨模型是读到了一份语义摘要，还是读到了一份只说
「这里曾有 38 条消息」的清单。

**摘要生成是一次真实的模型调用，按 §12.4 记账。** 它的调用次数、usage 与费用计入该 Run
所在 RunBudgetScope 的 consumed_model_calls/consumed_tokens/consumed_cost——价格按
§12.4 的钉价规则（主端点按 Run 创建时钉住的那份价格，Agent 声明的独立摘要端点没有钉价
机制，按当时生效的价格算）。只要拿到了答复就记账，不论 stop_reason 是不是
completed——一次拒绝或答非所问同样是在真实端点上花了钱、占了一次调用配额的调用。真的
不计的只有从未拿到 `ModelResponse` 的调用（异常，例如超时）：这不是因为「没打到
Provider」，这段代码分不清「没送达」和「送达但没等到答复」，只是没有答复可读、没有
usage 可算。累计后的数字要能在下一轮的 §12.4 预检里读到，使这次调用和其他任何一轮一样
能把 Run 推向调用次数或费用上限——一个摘要总是失败的会话不能每轮白付一次钱、白占一次
调用配额却不被算进任何上限。**这次调用单独记一条 RunEvent**，带上端点、模型、token 数
和费用，不并入 `CONTEXT_COMPACTED`：后者说的是「这一轮压缩发生了」，前者说的是「那次
生成调用花了什么」，复用一份已存摘要时压缩照常发生但不产生新的调用，两件事分开才不会
互相冒充。

内核不负责租户权限、Web 页面、飞书协议和 Docker 生命周期。

### 7.5 强制执行面

负责：

- 创建、续租和销毁 Docker 沙箱。
- 执行文件、命令、脚本和高风险工具。
- 限制资源、网络、挂载和密钥范围。
- 保存工作区产物。

平台可信进程的出站请求也必须经过统一安全执行面。M1 的模型端点和渠道技术验证使用可强制注入和静态检查的 `SafeOutboundClient`；M2 在引入 MCP、HTTP/OpenAPI 工具时升级为独立 `egress-proxy`。请求不能因由 API 或 Worker 发出就绕过网络策略。

沙箱失败时不得回退宿主机执行。

### 7.6 持久数据

- PostgreSQL：可信业务状态。
- MinIO/S3：上传文件、Agent 配置包、技能包和运行产物。
- Redis：任务唤醒、短期通知和实时事件加速，不作为 Run 最终状态来源。

## 8. 核心数据对象

### 8.1 身份与租户

- `Workspace`：租户和权限边界。
- `User`：管理后台用户。
- `AuthIdentity`：用户在 local 或 OIDC 身份提供方中的稳定 subject；User 可以绑定多个 AuthIdentity。
- `Membership`：用户在工作空间中的固定角色。
- `EndUser`：实际使用 Agent 的终端用户。
- `ExternalIdentity`：Web、飞书或上游应用身份映射。
- `ServiceAccount` 与 `ApiKey`：应用和自动化调用身份。

外部身份唯一键至少包含 `workspace_id + channel + external_user_id`。跨渠道合并身份必须显式绑定。

### 8.2 Agent 与配置

- `Agent`：可管理的 Agent 身份。
- `AgentVersion`：不可变的发布版本。
- `ModelPolicy`：主模型、备用模型和参数。
- `ModelEndpoint`：OpenAI 兼容、Anthropic 或 Gemini 端点及其上下文、输出、`context_accounting` 和 `usage_quality` 能力声明。
- `ModelPricingVersion`：输入、输出和缓存等单价、币种与生效时间。
- `ToolBinding`：Agent 获准使用的工具和权限。
- `ChannelBinding`：Web、飞书和 API 发布配置。
- `Secret`：加密保存的模型、工具和渠道凭证。

草稿可以修改；已发布 `AgentVersion` 不可原地修改。回滚通过重新激活旧版本完成。

Secret 使用信封加密：每个 Secret 由独立数据密钥 DEK 加密，DEK 再由部署侧密钥加密密钥 KEK 包装。数据库保存业务密文、包装后的 DEK 和 `key_id`，KEK 不能存入同一个数据库。

保存后，管理 API 只返回名称、作用域、更新时间和掩码，不再返回完整明文。轮换 KEK 时重新包装 DEK，旧 KEK 在全部 Secret 重包、校验和审计完成前不能删除。轮换任务必须可恢复；密钥还必须支持撤销和工作空间范围控制。

### 8.3 会话与运行

- `Session`：工作空间与 Agent 下的对话或任务容器；可以绑定经验证的 EndUser，也可以由 User 或 ServiceAccount 等非交互主体创建。它保存 `session_mode=ephemeral|persistent` 和当前 `head_run_id` 作为 FIFO 队首真相。
- `SessionWorkspace`：属于 Session 的持久逻辑文件空间，只对应沙箱中的 `/workspace/data`，文件保存在对象存储中并跨该 Session 的多次 Run 延续。
- `Run`：一次可观察、可取消的执行。
- `RunBudgetScope`：根 Run、重试链和后续子 Agent 任务树共享的预算与累计消费真相。
- `RunEvent`：模型、工具、子 Agent、审批、费用、错误和状态事件。
- `Approval`：等待人工决定的具体操作。
- `Artifact`：运行产生的文件和结果。
- `WorkerLease`：Worker 在一个执行时间片内对 Run 的排他执行权。
- `SandboxInstance`：实际运行工具的临时容器。
- `SandboxReservation`：Run 对某个 SandboxInstance 的独占保留关系，可以在执行时间片之间按 TTL 短暂保温。
- `SandboxBaseSnapshot`：M1 中指由平台发布流程预先构建、按镜像 digest 和 CPU 架构标识的只读运行时 Docker 镜像层，可在 Run 之间共享，但不得包含 Agent 专属依赖、用户数据、密钥或可写状态。根据 Agent 锁文件构建依赖层不属于 M1。
- `ObjectUpload`：WorkspaceRevision 提交前的对象上传登记，提供可枚举的 staging 前缀和清理状态。

每个 Run 必须属于一个 Session；没有交互会话的后台任务由系统创建专用 Session。WorkerLease 和 SandboxReservation 只属于 Run，SessionWorkspace 只属于 Session，不使用“Run 或 Session”二义持有关系。SandboxInstance 不得在不同 Run 之间共享可写状态。

子 Agent 使用独立 Run、独立 SessionWorkspace 和独立沙箱，通过 `parent_run_id`、`depth` 和 `delivery_status` 组成任务树。父子 Agent 的文件只能通过显式 Artifact 授权和对象存储传递，不能共享同一个可写挂载目录。

### 8.4 记忆与技能

- `UserProfileItem`：终端用户的稳定偏好。
- `MemoryItem`：经确认的长期事实，作用域为私有或共享。
- `SessionMessage`：完整原始会话消息。
- `Skill`：技能身份和元数据。
- `SkillVersion`：不可变技能内容、来源、哈希和扫描结果。
- `SkillProposal`：Agent 或开发者提出的新建或修改草稿。

### 8.5 治理与去重

- `AuditEvent`：谁在何时对哪个工作空间资源执行了什么管理动作及其结果。
- `IdempotencyRecord`：API 幂等范围、请求指纹、响应 Run 和过期时间。
- `ChannelEventReceipt`：渠道事件 ID、接收时间、处理状态和对应 Run。
- `DataDeletionJob`：用户数据清除范围、进度、统计和审计结果。

## 9. 租户与数据隔离规则

1. 关键业务数据必须带 `workspace_id`。
2. 服务端根据登录身份和经校验的工作空间选择解析租户：多工作空间管理用户通过 `X-Workspace-Id` 指明目标，服务端必须验证 Membership；API Key 从绑定关系推导工作空间，若同时携带 Header 则必须一致。不能信任请求体中任意传入的工作空间 ID。
3. 所有资源查询必须在数据访问层自动应用工作空间范围。
4. 即使调用方知道另一个工作空间的资源 ID，也必须被拒绝。
5. 平台管理员的跨工作空间访问必须使用管理权限并写审计日志。
6. Agent 模板可以克隆，但用户记忆、会话、文件、凭证和渠道身份不能复制。
7. 删除终端用户时，私有记忆、会话、文件和可识别信息进入可追踪清除流程。
8. 审计记录只保留业务所需的脱敏信息。
9. 共享记忆必须保留来源 Run 和来源用户；未经批准的用户内容不能进入共享记忆。
10. 工作空间可以配置会话、RunEvent、产物和审计的保留期限；清理任务必须记录数量、范围和结果。
11. 终端用户可以查看自己的会话，查看、更正或删除自己的私有记忆，导出本人数据，并提交删除请求；不能修改 Agent 共享记忆或审计记录。
12. M1/M2 可由已验证身份的管理员通过管理 API 代办上述数据权利请求，每次代办必须留下原请求人、范围和结果审计；M3 在 Web Chat 提供本人自助入口。

## 10. Agent 创建、测试和发布

### 10.1 Agent 配置内容

- 名称、简介、头像和人格。
- 默认模型和备用模型。
- 共享与私有记忆策略。
- 技能版本和工具权限。
- Goal 完成条件和质量门。
- 子 Agent 角色、工具范围和上限。
- 沙箱资源和网络策略。
- Web、飞书和 API 渠道配置。
- 各入口的 `interaction_mode=interactive|noninteractive` 及无交互主体时的审批解决策略。
- 时间、步骤和费用安全阀。

### 10.2 生命周期

Agent 状态为：

- `draft`：可编辑，不对生产流量生效。
- `published`：不可变，可接收运行请求。
- `disabled`：不接受新 Run，历史数据仍可查看。

发布前必须完成配置校验。发布后生成唯一版本号，并能查看与上一版本的差异。

### 10.3 配置即代码

Agent 可以导入或导出声明式配置包。配置包包含人格、模型别名、策略、技能引用和工具绑定，不包含：

- 模型和工具真实密钥。
- 终端用户记忆。
- 会话和 Run。
- 渠道凭证。

仪表盘和配置文件操作同一份领域数据，不维护两套互相漂移的配置。

配置包必须带 Schema 版本：

```yaml
apiVersion: tiny-hermes.io/v1alpha1
kind: Agent
metadata:
  name: research-agent
spec:
  modelAlias: primary
  personality: |
    You are a careful research agent.
```

AgentVersion 保存原始配置包、`schema_version`、规范化运行快照、内容校验和及迁移记录。平台通过显式迁移器读取仍受支持的旧 Schema；回滚使用原版本的规范化快照，不能要求用户先把旧版本重新发布。导入不支持的 Schema 时返回明确错误和支持版本列表，不能静默丢弃字段。

## 11. 统一 Run 模型

### 11.1 适用范围

通过入口校验并被正常受理的以下请求都会创建 Run；§18.1 中因持久 Session 已阻塞而返回 409 的 Chat Completions 请求不属于已受理请求，不创建 Run：

- Web Chat 消息。
- 飞书消息。
- OpenAI 兼容 Chat 请求。
- 后台自主任务。
- 子 Agent 委派。

短任务可以等待 Run 并流式返回；长任务立即返回 Run ID，在后台执行。

同一 Session 中的 Run 按单调递增的 `session_sequence` 组成 FIFO。平台必须始终维持以下不变量：`Session.head_run_id` 指向该 Session 中 `session_sequence` 最小的非终态 Run；不存在非终态 Run 时为 `null`。只有 head Run 可以被 Worker 领取；后续非终态 Run 即使状态为 `queued`，也必须保存 `blocked_by_run_id=head_run_id` 并保持不可调度。Worker 领取查询必须同时校验 Run 是所属 Session 的队首，不能只按 `queued` 状态领取。

head Run 从 `queued`、`running`、`waiting_approval`、`waiting_external`、`paused`、`cancelling` 到 `interrupted` 都持续占用 Session，直到进入 `completed`、`failed` 或 `cancelled` 才让出队首。用户连续发送的普通消息分别创建 Run，不合并、不并行；暂停、审批、继续和取消等控制命令可以越过消息队列。不同 Session 可以并行执行。

当 head Run 处于 `paused` 或 `waiting_*` 时，Runs API、Web Chat 和飞书可以保存新的 pending Run，但必须立即返回 `session_blocked`、`blocked_by_run_id`、暂停或等待原因、排队位置和当前主体可执行的批准、拒绝、继续或取消操作，不得静默排队。Chat Completions 绑定持久 Session 时采用 §18.1 的兼容规则：直接返回 409，且不创建 pending Run。用户可以显式新建 Session 开始不相关任务。这些规则保证 SessionWorkspace、记忆提案和上下文顺序稳定。

head Run 进入终态时，平台必须在同一数据库事务中锁定 Session，选出 `session_sequence` 最小的后续非终态 Run；存在时把它设为新 head，并把新 head 的 `blocked_by_run_id` 置空，把其他非终态 Run 的 `blocked_by_run_id` 更新为新 head ID；不存在时把 `Session.head_run_id` 置为 `null`。事务提交后才能唤醒 Worker。已是 `completed`、`failed` 或 `cancelled` 的 Run 永远不能被选为新 head。事务失败时原 head Run 不得只改终态而留下错误队首。

### 11.2 状态

Run 使用以下状态：

- `queued`
- `running`
- `waiting_approval`
- `waiting_external`
- `paused`
- `cancelling`
- `interrupted`
- `completed`
- `failed`
- `cancelled`

`completed`、`failed` 和 `cancelled` 是终态。`paused` 使用 `pause_reason` 区分 `manual`、`limit`、`approval_expired`、`approval_rejected`、`approval_unavailable`、`external_timeout`、`context_overflow`、`tool_budget_exceeded`、`compat_timeout`、`operator` 和 `system`。

Run 还保存 `state_version`、`next_event_sequence`、`session_sequence`、`blocked_by_run_id`、`pause_requested_at`、`cancel_requested_at`、`wait_kind`、`wait_policy`、`wait_deadline_at`、`retry_of_run_id`、`budget_root_run_id` 和当前检查点步标识，其中不适用字段为空。每次状态变化必须使用状态版本做并发检查，先持久化，再通过 SSE 和内部通知发布。

### 11.3 权威状态转换矩阵

没有列在表中的转换一律拒绝并记录审计事件。

一个“检查点步”是一个可单独保存结果的安全步骤：一次模型调用、一次工具调用或一次 Goal 判断。每步结束时先持久化消息、工具结果、外部副作用标记、如有变更则提交 SessionWorkspace 新版本，并保存下一步。检查点步只定义恢复粒度，不自动导致 `running → queued`。

一个“执行时间片”是 Worker 的调度粒度，从领取 WorkerLease 开始，到一个 Goal 轮结束或到达 `max_slice_seconds` 后的下一个检查点步结束时停止，两者取先到者。一个 Goal 轮包含一次模型决策、由该决策产生的零个或多个工具调用和随后的 Goal 判断；尚未启用独立 Goal 判断器的 M1 Agent 以当前一轮模型决策及其工具调用全部完成作为轮边界。时间片到期不会强行中断已在执行的工具调用。

| 当前状态 | 可进入状态 | 触发条件 |
|---|---|---|
| `queued` | `running` | Worker 成功领取租约 |
| `queued` | `paused` | 尚未执行时收到人工暂停请求 |
| `queued` | `cancelled` | 尚未执行时收到取消请求 |
| `running` | `waiting_approval` | 产生尚未批准的具体操作 |
| `running` | `waiting_external` | Goal 判断为等待且给出唤醒条件或截止时间 |
| `running` | `queued` | 异步 Run 的当前执行时间片在检查点结束，且仍有下一步，包括 Goal 返回 `continue` 或时间片到期；同步 Chat Completions Run 在 §18.1 规定的交付窗口内不因普通轮边界重新排队 |
| `running` | `paused` | 到达安全点后处理人工暂停、安全阀或上下文溢出 |
| `running` | `cancelling` | 收到取消请求，当前步骤需要清理 |
| `running` | `completed` | 完成条件和质量门均通过 |
| `running` | `failed` | 可归因且不可重试的失败，且外部结果明确 |
| `running` | `interrupted` | Worker、沙箱或连接异常导致当前步骤结果不明确，或者检查点结果无法安全提交，例如 SessionWorkspace CAS 冲突 |
| `waiting_approval` | `queued` | 审批通过，重新进入调度队列 |
| `waiting_approval` | `paused` | 审批拒绝、过期或收到人工暂停 |
| `waiting_approval` | `cancelled` | 等待审批期间取消 |
| `waiting_external` | `queued` | 外部条件满足 |
| `waiting_external` | `paused` | 等待超时或收到人工暂停 |
| `waiting_external` | `cancelled` | 等待外部条件期间取消 |
| `paused` | `queued` | 有权限的主体继续运行 |
| `paused` | `cancelled` | 暂停期间取消 |
| `cancelling` | `cancelled` | Worker 完成安全清理 |
| `cancelling` | `interrupted` | 清理期间失去 Worker，结果不明确 |
| `interrupted` | `queued` | 证明可以从已保存安全步骤重试 |
| `interrupted` | `failed` | 存在无法确认或不能重放的外部副作用 |
| `interrupted` | `cancelled` | 有权限的主体决定放弃恢复 |

`waiting_external` 必须至少设置事件订阅条件或 `wait_deadline_at`。截止时间到达且条件仍未满足时转为 `paused(external_timeout)`，不能永久占用等待队列。

`waiting_approval`、`waiting_external` 和 `paused` 均保证没有活动 WorkerLease、SandboxReservation 或 SandboxInstance。进入这些状态前，Worker 必须到达安全点、保存检查点、提交 SessionWorkspace、销毁沙箱并释放 WorkerLease；销毁无法确认时进入 `interrupted`，不能伪装成等待或暂停。因此三个状态可以直接取消，不需要经过 `cancelling`。

### 11.4 暂停和取消语义

首版不增加公开的 `pausing` 状态。暂停请求只写入 `pause_requested_at`；Worker 在当前检查点步结束后的安全点转为 `paused(manual)`。正在执行的外部写操作不能被强行伪装成已暂停。

取消允许协作式中止。若当前步骤不能安全中止，先进入 `cancelling` 并等待结果或清理；管理员强制终止 Worker 时，Run 进入 `interrupted`，而不是直接标记 `cancelled`。

### 11.5 Run 创建幂等

运行 API 接受 `Idempotency-Key`。数据库必须对 `workspace_id + caller_type + caller_id + endpoint + idempotency_key` 建唯一约束。创建流程先尝试插入幂等记录，再创建 Run；并发冲突时等待并读取已提交记录，不能用“先查询、后插入”的方式判断：

`caller_type + caller_id` 是不随凭证轮换变化的调用主体：Web 和飞书使用 `EndUser + EndUser.id`，管理用户使用 `User + User.id`，服务调用使用 `ServiceAccount + ServiceAccount.id`。`ApiKey.id` 不作为 `caller_id`，换 Key 不得使幂等范围失效。

- 相同 Key 和相同请求指纹返回原 Run。
- 相同 Key 但请求指纹不同返回 `409 Conflict`。
- API 幂等记录在关联 Run 进入终态前不得过期；终态后默认再保留 24 小时，工作空间可以在 1 小时到 7 天范围内配置额外保留期。
- 飞书等渠道必须以 `channel_binding_id + channel_event_id` 去重，渠道事件记录保留 7 天。
- 重复渠道事件返回成功确认，但不能创建第二个 Run、重复扣费或重复执行外部操作。

### 11.6 事件

RunEvent 至少记录：

- Run 创建和状态变化。
- 使用的 Agent、模型、技能和工具版本。
- 模型请求和响应的脱敏摘要。
- 工具调用、实际参数摘要、结果和耗时。
- 子 Agent 创建、状态和结果交付。
- Goal 判断和质量门结果。
- 审批请求与决定。
- Token、预计费用和延迟。
- 错误、重试和备用模型切换。
- 产物引用。

事件具有单调递增序号。SSE 断线后可以从最后事件序号继续读取。

所有 RunEvent 写入者通过 Run 上的 `next_event_sequence` 原子预留一个序号或连续序号段，再在同一数据库事务写事件。API、Worker 和 scheduler 不得使用 `max(sequence) + 1`。唯一约束冲突必须回滚并有限重试整个事件事务，不能丢弃事件后继续。

如果客户端游标早于保留期内最早可用事件，SSE 接口返回 `410 Gone`，同时返回最早可用序号、当前 Run 快照地址和建议的重新同步方式，不能返回一个看似连续但已经缺失中间事件的流。

## 12. 自动 Goal 循环

### 12.1 判断结果

每轮 Agent 执行后，Goal 判断器返回：

- `done`：满足完成条件，结束 Run。
- `continue`：任务未完成，生成下一轮指令。
- `wait`：等待时间、外部事件或人工操作。

模型的判断只是建议，服务端必须验证状态、预算和权限。

**`continue` 让位于中途到达的用户消息。** 当同一 Session 中存在一个**在当前 Run 开始
之后**创建的排队 pending Run 时，判为 `continue` 的 Run 不得继续下一轮，而是以
`completed` 结束、让出队首，使那条消息立即得到处理。

**判据是「在我开始之后才到」，不是「后面有人排着」。** 用户连续发出的多条消息各自
创建 Run（§566），它们在第一条开始执行前就已经排好；那不是插话，只是排队。若以
「后面有人排着」为判据，连发三条时前两条会各自只跑一轮就被截断——一个需要多轮才能
答完的问题会以 `completed` 结束而根本没答完。让位要接管的是「我干活的过程中你说了话」，
参照系因此是当前 Run 的开始时间。

这是有意的取舍：用户在任务跑到一半时说的话，几乎总是「我要改主意」或「这不对」，
而让他等到任务自己跑完才被听见，是把机器的进度排在人的意图前面。上游 Hermes 的同一条
规则是 `any real user message preempts the continuation loop`。

**状态是 `completed`，但 `goal` 文档必须记下它是被打断的**（`preempted` 字段），
`goal_outcome` 不因此改写、仍然是判断器本轮给出的裁决（通常是 `continue`，连同
`unmet` 一起留着）。一个没达成目标的 Run 在列表里显示为 `completed` 已经够容易
误读，若连原因都不留，运维就只能猜；但让位是平台的决定，不是判断器改口，两者不
应该在同一个字段里打架（v2.9.2）。

**不适用于「停」的语义。** 取消、暂停这类控制命令按 §566 仍然越过消息队列，且不依赖
本条——一条要求停止的指令是否被执行，不能取决于模型或 Goal 判断器同不同意。

**显式的「不打断注入」是另一件事，本版不做。** 上游提供 `/steer`，把一段话追加到当前
工具结果之后、不打断循环，由模型自行决定如何处理。它需要精确的注入时机（工具调用与
结果不能拆开）并且会打破「一轮发送什么在调用前就定好」这条约束，因此单列，不与本节的
抢占混为一谈。

### 12.2 完成条件

任务可以声明：

- 预期产物。
- 验证命令。
- 必须满足的约束。
- 允许操作的范围。
- 停止条件。

验证命令必须在沙箱内执行，不允许在宿主机运行。

### 12.3 安全阀

开发者不需要为每个 Run 手工填写限制，但平台管理员可以设置默认和最大值：

- 最大累计执行时间：只累计持有有效 WorkerLease 并实际执行的时间，M1 默认 15 分钟。
- 最大墙钟期限：从根 Run 创建到当前的自然时间，M1 默认 24 小时，用于限制长期排队、反复调度和重试链；暂停和等待另受各自 TTL 约束。
- 最大连续轮数。
- 同一 Session 中出现排队消息时，自动续跑立即让位（见 §12.1）。
- 最大模型 Token 或预计费用。
- 最大工具调用数。
- 最大并行子 Agent 数。
- 最大派生重试次数，默认为 3。

达到累计执行时间或墙钟期限时，不得开始新的模型或工具调用。`queued` 或 `running` Run 在安全点进入 `paused(limit)`；已处于 `paused`、`waiting_*` 或 `interrupted` 的 Run 保持当前状态，但在未由有权限主体明确扩大预算前禁止继续调度，并写 `run_limit_reached` 事件。

每个根 Run 创建一个预算范围，由 `budget_root_run_id` 标识。根 Run、子 Agent Run 和通过 `retry_of_run_id` 派生的所有重试共享累计 Token、费用、工具次数、执行时间和重试次数。新重试只继承根预算的剩余额度，不得因创建新 Run 重置安全阀；扩大预算必须由有权限的主体明确操作并写审计。

从 `failed` Run 派生“重试安全步骤”时必须确认最后检查点可以安全继续，没有结果不明确的外部副作用。M1 还要求来源是该 Session 最新 Run，且 SessionWorkspace 当前 revision 仍等于来源检查点 revision；否则返回 `retry_context_stale` 并引导用户显式新建 Session，不能把旧上下文插回已经前进的会话。新 Run 进入原 Session 的 FIFO，保存 `retry_of_run_id`，并继承原 `budget_root_run_id`。重试计数在根预算范围内原子递增，默认最多 3 次；并发点击不能分别得到一份新预算。

### 12.4 Token 与费用安全阀

每个模型端点必须声明上下文窗口、最大输出和 `usage_quality`：

- `provider`：Provider 返回可靠 usage，平台保留原始值和规范化值。
- `estimated`：Provider 没有可用 usage，平台用与该模型匹配的本地 tokenizer 估算，并在 RunEvent、API 和 UI 明确标记为估算值。
- `unavailable`：Provider 不返回 usage，也没有经验证的匹配 tokenizer，不得伪造精确统计。

M1 必须实现 Token、调用次数、最大单次输出和时间上限。`usage_quality=unavailable` 时禁用 Token 和货币费用硬上限，但仍强制时间、调用次数和单次最大输出；要求严格 Token 或费用上限的任务不能选用该端点。未知 usage 和未知价格都不等于 0。

ModelPricingVersion 记录币种、输入、输出、缓存等单价、生效时间和维护人。OpenAI 兼容私有端点由管理员手工配置；未配置价格时显示“费用未知”，不能把未知费用记为 0。管理员可以明确把本地模型价格设置为 0，这与“未知”是两个不同状态。

Run 创建时固定引用当时的 ModelPricingVersion；运行中修改模型别名或价格不会重算已经开始的 Run，保证安全阀和用量记录可以追溯。

每次模型调用前根据预计输入和最大输出做预检，调用后按 Provider usage 或已标记的本地估算修正累计值。流式调用的最终 usage 可能只在结束时可得，因此 Token 和货币上限允许最多被单次调用的实际用量越过；文档和 UI 必须说明这一边界。

**三个阀门是一件事一起遵守，不是选两个。** 调用次数、Token 与费用同属一次预检要看护的
安全阀，一次调用只移动其中一两个没有原则支持——尤其调用次数计数器是唯一一个在「没配价格、
也没设 max_cost」这种默认部署形态下仍然生效的阀门，只记 Token 和费用会让这种最常见的形态
完全不设防。§7.4.2 的摘要生成调用是这条规则第一次遇到「同一次调用，两个可能不同的端点」：
它默认打到 Agent 自己的主端点，此时按这个 Run 创建时钉住的那份 ModelPricingVersion 算，
不是现读现算的价格——现读现算会让同一个端点在同一个 Run 里因为两次调用发生的时机不同而
按两个价格计费，正是这一段开头「运行中修改…不会重算已经开始的 Run」要防的事。只有 Agent
显式声明了一个不同的摘要端点时，才没有钉住的版本可读，只能按当时生效的价格算——调用次数
不受这个区分影响，两种情况下都照常计数。

声明的独立摘要端点若没有配置价格，这次调用的费用按 unknown 记账，会让整棵 Run 树的
consumed_cost 从此永久变成 unknown（与主端点未定价时同一条规则，未知费用不等于 0，也
不会被后续调用洗白）；工作空间设了费用上限的话，下一轮预检会因为「未知费用遇到费用
上限」拒绝这个 Run——可见的拒绝，不是静默的记账错误。发布校验（§7.4.2）只检查声明的
摘要端点窗口是否够大，不检查是否已经定价：和主端点未定价可以发布一样，这是允许发布的
状态，后果由这条记账规则自己说清楚，而不是在发布时堵住。

## 13. 一层父子 Agent 委派

1. 主 Agent 可以创建多个并行子 Agent。
2. 子 Agent 是独立 Run，具有独立 Session、SessionWorkspace、事件、沙箱和费用记录。
3. 首版 `depth` 最大为 1，子 Agent 不能继续创建孙 Agent。
4. 子 Agent 继承父 Run 的 `end_user_id` 用于身份、审计和数据归属，但不会继承父 Agent 的私有记忆内容。
5. 子 Agent 读取的是 `workspace + 子 Agent + end_user` 范围内的私有记忆；读取和提出写入还必须得到 `delegation_scope` 中的 `memory.read_private` 或 `memory.propose_private` 权限。
6. 子 Agent 权限是父 Agent 权限与委派策略的交集，范围覆盖工具、文件、网络、密钥、技能和记忆，不能扩大权限。
7. 主 Agent 只接收子 Agent 的结构化结果和明确授权的 Artifact 引用，不自动吸收完整上下文。
8. 父 Agent 向子 Agent传文件时先把选定文件保存为 Artifact，并创建只读授权；子 Agent 产物上传对象存储后，再由平台授予父 Run 读取权。父子 Agent 不共享可写目录。
9. 子 Agent 结果投递使用幂等键；父 Run 暂时不可用时结果继续保留并重试。
10. 父 Run 等待子 Run 时进入 `waiting_external`，设置 `wait_kind=child_runs`、`wait_policy=all|any`、子 Run ID 集合和 `wait_deadline_at`，释放 WorkerLease 并销毁 SandboxReservation 与 SandboxInstance。`all` 在全部子 Run 进入终态后唤醒，`any` 在首个子 Run 成功完成后唤醒并默认取消其余子 Run；如果全部子 Run 都已终止且无成功结果，父 Run 仍回到 `queued` 并收到结构化失败摘要。截止时间到达时按 `paused(external_timeout)` 处理。
11. 取消父 Run 时，默认级联取消仍在运行或等待的子 Run。
12. 整棵任务树与其重试链共享同一 `budget_root_run_id` 下的总费用、Token、执行时间、工具次数、重试次数和并行上限。

## 14. 记忆

### 14.1 私有记忆

按 `workspace + agent + end_user` 隔离，包含用户偏好和长期事实。

工作空间策略可以选择：

- 关闭自动记忆。
- 所有记忆候选等待审批。
- 允许低风险私有记忆经规则检查后自动写入。

### 14.2 共享记忆

属于 Agent，所有终端用户可能受其影响。

共享记忆只能来自管理员直接编辑或经过明确批准的提案。原始用户消息不能默认进入共享记忆。

### 14.3 会话搜索

完整会话消息保存在数据库中，首版使用 PostgreSQL 全文搜索。过去会话按需检索，不全部进入模型上下文。

被用户撤回的消息（见 §19.4）对会话搜索不可见。理由是撤回本身要成立：压缩摘要会主动告诉模型这段历史「searchable with `session.search`」并附上线索词，若被撤的内容还能搜回来，撤回就是漏的。它们仍然留在库里、仍然出现在转写记录中并标记为已撤回——这与 §344 的擦除是两件事，擦除的消息对转写记录也不可见。

首版不建设知识图谱、复杂向量记忆和多个外部记忆提供方。

## 15. 技能与自我改进

### 15.1 企业内部技能目录

首版提供：

- 搜索、查看和绑定技能。
- 人工上传技能包或从 Git 导入。
- 来源、内容哈希和扫描结果。
- 不可变版本、停用和回滚。
- 平台内置技能和工作空间私有技能。

首版不运营公共市场。

### 15.2 渐进加载

系统提示词只列出已授权技能的名称和简介。Agent 命中任务后，才加载对应 `SKILL.md` 和必要参考文件。

### 15.3 Agent 自我改进

Agent 可以：

- 提出新技能。
- 提出已有技能的补丁。
- 写入配套参考资料、脚本和模板草稿。

变更流程固定为：

1. 创建 `SkillProposal`。
2. 生成与目标版本的差异。
3. 进行静态扫描和权限检查。
4. 人工批准或拒绝。
5. 批准后发布新的不可变版本。
6. Agent 明确切换到新版本。

Agent 不得直接修改正在生产运行的技能版本。

## 16. 工具、审批与沙箱

### 16.1 首版工具

这里的“首版”指完整的 0.3 Enterprise Preview，不表示 M1 一次性交付全部工具：

| 工具 | 首次交付 |
|---|---|
| 文件读写 | M1 |
| 受限命令执行 | M1 |
| 记忆读写和会话搜索 | M2 |
| HTTP 请求 | M2 |
| MCP Server | M2 |
| 从 OpenAPI/HTTP 描述生成的业务工具 | M2 |

### 16.2 两次权限检查

1. 工具 schema 暴露给模型前，检查工作空间、Agent、用户和运行策略。
2. 工具真正执行前，再检查实际参数、审批、密钥和沙箱状态。

人格、系统提示词和技能不是安全策略，不能替代服务端检查。

MCP 必须显式绑定允许的工具子集，不得默认把 Server 发现的所有工具交给模型。平台缓存 Server 能力和 schema 哈希，每次运行前重新校验已绑定子集。如果运行时 schema 变化并超过工具预算，执行层写入 `tool_schema_budget_exceeded` RunEvent，不能截断 schema 或继续调用；M2 在没有事先声明的安全子集时直接进入 `paused(tool_budget_exceeded)`，基于相关性的自动选工具放到后续版本。

### 16.3 审批

审批分为两类：

- `user_confirmation`：终端用户只能确认自己发起、只影响自己数据且不会扩大权限的操作，例如向自己的会话发送结果或覆盖自己的工作文件。
- `governance_approval`：网络、密钥、工作空间资源、共享记忆、生产技能、生产渠道、付款和高风险删除，只能由工作空间管理员或平台管理员批准。

`user_confirmation` 只能由发起该 Run 的 EndUser 本人决定，管理员和开发者不能伪装用户代批。确有业务必要的管理覆盖必须新建一个独立 `governance_approval`，写明理由、影响范围和决定人，不得改写原用户确认记录。

`interaction_mode=interactive` 的 Run 只有在绑定了经验证 EndUser 时才能使用 `user_confirmation`。ServiceAccount 发起的 `noninteractive` Run 不能伪造 EndUser 确认；AgentVersion 发布时必须为其每个需要用户确认的工具显式选择“禁用”、“工作空间管理员批准的狭范围预授权”或 `governance_approval`，否则发布失败。运行时仍意外缺少可确认主体时进入 `paused(approval_unavailable)`，不静默升级或代批。

以下操作默认要求治理审批或由工作空间策略明确放行：

- 删除或覆盖共享、生产或他人文件；覆盖本人工作文件可按 `user_confirmation` 处理。
- 发送外部消息。
- 创建订单、付款或其他不可逆业务操作。
- 扩大网络、文件或密钥权限。
- 无法判断前一次是否成功的重复写操作。

Approval 保存 `approval_type`、`required_permission`、`requested_by`、`expires_at`、规范化参数哈希、决定人、决定时间和决定原因。默认有效期 24 小时，工作空间可以在 5 分钟到 7 天范围内配置。

审批绑定规范化后的真实参数。工具名、参数、工作目录、目标资源或权限发生变化时，原批准失效。审批通过后 Run 回到 `queued`；审批拒绝或过期时进入 `paused(approval_rejected)` 或 `paused(approval_expired)`，允许有权限的人修改输入后重新发起审批。终端用户绝不能批准工作空间治理操作。

M1 不交付审批系统的前提是：只提供当前 Session `/workspace/data` 内的文件操作和无网络沙箱命令，全部作用于调用主体正在使用的工作区，并由 AgentVersion 在发布时预先绑定。M1 不允许访问共享、生产或他人文件，不允许外部写操作，也不允许运行中扩大权限；请求一旦超出这些边界就直接拒绝，而不是等待审批。M2 在引入共享资源、联网工具和外部副作用之前必须交付本节审批能力。

### 16.4 Docker 最低要求

- 非 root 用户。
- 只读根文件系统。
- 删除不必要的 Linux capabilities。
- `no-new-privileges`。
- CPU、内存、磁盘、进程和时间限制。
- 默认无外网。
- Agent 级出网域名白名单必须是工作空间已批准集合的子集。
- 只把当前 Run 所属 SessionWorkspace 的指定版本挂载到 `/workspace/data`；子 Agent 使用独立工作区版本。
- 密钥短期注入，不写入镜像、文件、提示词或日志。

Docker 控制权只授予可信的 sandbox-controller。API、Web、Agent 沙箱和模型均不得直接访问 Docker socket。

沙箱内的可写路径分为三类：

- `/workspace/data`：SessionWorkspace 的持久数据，包括用户文件、源码、产物、依赖锁文件和可重建配置。对象存储中的不可变 `workspace_revision` 是它的可恢复真相。
- `/workspace/cache`：npm/pip 依赖树、虚拟环境和构建缓存。不进入 `workspace_revision`，只保证在当前保温 SandboxInstance 生命周期内存在；冷恢复后允许按 `/workspace/data` 中的锁文件重建，不能对用户宣称已经恢复。工具已启用的 Run 在每个执行时间片第一次模型调用前先 acquire 或恢复沙箱；结果必须返回 `cache_state=reused|reset`。为 `reset` 时，Worker 先写 `sandbox_cache_reset` RunEvent，并注入不可由历史消息覆盖的结构化运行时提示，再调用模型。
- `/tmp`：完全临时的数据。沙箱销毁后不保留。

运行环境和命令工具应默认把依赖安装目录、虚拟环境和构建缓存导向 `/workspace/cache`，把依赖锁文件和重建配置保存在 `/workspace/data`。如果用户明确把大型依赖树写入 `/workspace/data`，平台按持久数据处理并受提交耗时、磁盘与文件数安全阀约束，不能悄悄跳过检查点。

创建 SandboxInstance 时把指定 `workspace_revision` 物化到 `/workspace/data`。每个可能修改 `/workspace/data` 的文件或命令工具成功后，Worker 必须在把该检查点步标记为完成之前，上传该目录的增量并使用当前 `workspace_revision` 做比较后交换（CAS）提交新版本；对 `/workspace/cache` 的普通变更不做逐检查点提交。Session 队首规则保证正常情况下只有一个持久数据写入者；仍出现版本冲突时写入 `workspace_conflict` 事件并进入 `interrupted`，不得覆盖新版本。

每批待提交对象使用 `staging/{workspace_id}/{upload_id}/` 键前缀，并先登记 ObjectUpload。WorkspaceRevision 提交事务把对应登记标为 `committed`；scheduler 只扫描超过默认 24 小时仍未提交的登记，确认没有 revision 引用后按前缀删除。不能依赖全桶扫描猜测孤儿对象。

M1 可跨 Run 共享的 `SandboxBaseSnapshot` 只包含平台发布流程构建并固定 digest 的运行时 Docker 镜像层。M1 不读取 Agent 锁文件构建依赖快照，也没有运行时快照构建服务、容量管理或淘汰流程；Agent 依赖在各 Run 的 `/workspace/cache` 中安装，冷启动后按锁文件重建。每个 Run 必须新建独立可写层；不同 Run 之间不能共享 `/workspace/cache`、`/tmp`、后台进程、密钥或任何可写状态。

`running → queued` 的执行时间片边界只释放 WorkerLease。释放前 sandbox-controller 必须冻结 SandboxInstance，使其不再获得 CPU 时间也不能发起新网络连接；冻结失败时 Run 进入 `interrupted`，不得伪装成已重新排队。SandboxReservation 可以继续归属该 Run，使冻结的 SandboxInstance 在可配置 `sandbox_idle_ttl` 内保温；只有同一 Run 的后续时间片获得新 WorkerLease 后才可解冻。TTL 到期由 scheduler 在确认工作区已提交后销毁。进入 `waiting_*`、`paused` 或任何终态时不保温，必须销毁实例并结束保留关系。

### 16.5 统一出站与 SSRF 防护

HTTP/OpenAPI 工具默认在沙箱中执行。有效出站范围是“平台允许范围 ∩ 工作空间允许范围 ∩ Agent 白名单 ∩ 本次 Run 或委派范围”，必须由出站执行面在连接时强制计算，不能依赖管理界面自觉遵守。

M1 不交付 MCP、OpenAPI 或通用沙箱联网工具，沙箱保持无网络。ModelEndpoint 和飞书技术验证等可信进程的动态出站地址必须注入平台统一 `SafeOutboundClient`；出站包之外直接创建原始 HTTP 客户端或 socket 的代码在静态检查和测试中拒绝。M2 在引入 MCP 和 OpenAPI/HTTP 工具之前必须上线独立 `egress-proxy`，使 API、Worker、sandbox-controller 和沙箱的动态出站目标经过网络级强制边界。

强制边界必须在每次连接和每一次重定向时重新解析、校验并固定实际 IP，默认拒绝 loopback、link-local、云元数据地址和未批准的私有网段，防止 DNS rebinding。跨 origin 重定向不得携带原请求的授权头或密钥。平台管理员可以显式批准企业私有端点或网段；工作空间管理员只能在已批准范围内选择目标，不能自行打开内网。

## 17. 错误、重试和恢复

### 17.1 可以自动重试

- 模型限流和短暂网络故障。
- 明确只读工具的临时失败。
- 子 Agent 结果向父 Agent 投递失败。
- Worker 在确认尚未产生外部副作用前崩溃。

自动重试必须包含退避、次数上限、幂等键和结构化事件。

### 17.2 必须暂停或审批

- 外部写操作结果不明确。
- 需要扩大权限。
- 达到安全阀。
- 沙箱状态异常。
- Goal 判断与质量门相互矛盾。

### 17.3 恢复边界

- 保存会话不等于恢复正在执行的代码。
- Worker 崩溃后只能从已保存的安全步骤重新调度。
- 外部副作用不能盲目重放。
- 上下文压缩失败时必须保留原始消息；若原文无法装入目标模型窗口，进入 `paused(context_overflow)`，不能静默删除或继续请求 Provider。
- 已完成子 Agent 的结果应持久保留，等待父 Run 恢复后交付。
- `interrupted` Run 在证明安全时可以回到 `queued` 继续原 Run；`failed` 是终态，UI 中的“重试安全步骤”会创建新 Run，写入 `retry_of_run_id` 并显式复用获授权的上下文或产物，原 Run 仍保持 `failed`。

## 18. API

### 18.1 运行 API

首版至少提供：

- OpenAI 兼容的 Chat Completions 接口。
- 创建 Run。
- 查询 Run。
- 订阅 Run SSE 事件。
- 暂停、继续和取消 Run。
- 从 `failed` Run 派生“重试安全步骤”的新 Run。
- 创建和查询持久 Session。
- 查询和提交审批。
- 获取 Run 产物。
- 查询 Agent 公开信息。

创建 Run 和 Chat Completions 均接受 `Idempotency-Key`。

Runs API 或 Playground 向已被 `paused` 或 `waiting_*` head Run 阻塞的 Session 创建普通 Run 时，仍然创建 pending Run，首次创建返回 `201 Created` 和 Run 快照；快照的 `queue` 字段必须包含 `status=session_blocked`、`blocked_by_run_id`、`queue_position`、head 状态与暂停或等待原因，以及当前主体对 head 可执行的 `available_actions`。这不是 HTTP 错误。只有下述 Chat Completions 持久 Session 路径返回 409 且不创建 Run。

Chat Completions 默认按无状态兼容方式工作：每个请求创建一个 `session_mode=ephemeral` 的一次性 Session，并以请求携带的完整 `messages` 作为本次上下文。一次性 Session 仍持久化 Run、事件和审计记录，但后续请求不会自动复用它的历史或 SessionWorkspace。API Key 代表调用主体和授权范围，不能被自动当作共享 Session 键；OpenAI 请求体中的 `user` 字段是调用方提供的业务标签，不能作为可信 EndUser 身份或 Session 键。

需要连续会话时，调用方必须先通过 Sessions/Runs API 创建或取得 `session_mode=persistent` 的 Session，并在 Chat Completions 请求中显式传入 `X-Tiny-Hermes-Session-Id: ses_xxx`。服务端必须验证该 Session 属于当前工作空间和 Agent，且当前调用主体有权使用；验证失败不能退回一次性 Session。平台不根据消息内容、API Key 或 `user` 字段猜测会话归属。

如果显式绑定的持久 Session 已被 `paused` 或 `waiting_*` 的 head Run 阻塞，Chat Completions 在创建新 Run 前返回 HTTP `409 Conflict` 和 OpenAI 风格 error，错误码为 `session_blocked`；扩展字段至少包含 `session_id`、`blocked_by_run_id`、`head_status`、`available_actions` 和 `runs_api_url`。该请求不创建 pending Run，避免兼容客户端重试造成排队堆积。`session_blocked` 表示请求开始前已有任务阻塞；`requires_runs_api` 表示本次兼容 Run 已经创建，随后才进入兼容接口不能承载的等待、审批或暂停状态。

OpenAI 兼容接口只承诺有时限的常用聊天和流式语义。AgentVersion 只有在启用 `chat_completions` 交付模式、配置 `sync_timeout_seconds <= 60` 且未绑定必需治理审批的工具时，才可以通过该接口调用。全局默认值和硬上限都是 60 秒，工作空间只能调低。长期 Goal、外部等待、任务树和人工审批使用 Runs API，不强塞进 Chat Completions。

为避免同步响应在普通轮边界重新排队，Chat Completions 创建的同步 Run 在 `sync_timeout_seconds` 内可以连续持有 WorkerLease，不执行异步 Run 的普通 Goal 轮时间片切换。它只在完成、进入 `waiting_*`/`paused`/审批、收到取消请求或同步时限到期时释放 WorkerLease；已经开始的工具调用仍必须先到安全检查点。该例外只改变短时同步交付的调度方式，不放宽 60 秒硬上限，也不允许兼容接口承载长期任务。

兼容请求意外进入 `waiting_approval`、`waiting_external` 或 `paused` 时，平台停止该兼容调用并返回 OpenAI 风格 error 对象，错误码为 `requires_runs_api`，扩展字段包含 `run_id`。达到 `sync_timeout_seconds` 时立即记录暂停请求；已在执行的检查点步保存明确结果后进入 `paused(compat_timeout)`，此后不得开始新的模型或工具调用。流式响应在响应头已经发送后使用明确的 `error` SSE 事件并结束连接；这属于 tiny-hermes 扩展，文档必须说明严格 OpenAI 客户端应改用 Runs API。

`paused(compat_timeout)` 默认保留 24 小时，期间可由有权限的主体在 Runs API 中继续或取消。如果 24 小时内没有操作，`scheduler` 将其转为 `cancelled`，防止兼容超时 Run 无限堆积。

### 18.2 管理 API

管理 API 独立于运行 API，负责：

- 工作空间、成员和角色。
- Agent、版本和发布。
- 模型、工具、技能和渠道。
- 密钥和 API Key。
- 记忆、会话、用量和审计。

### 18.3 API Key

API Key 绑定工作空间，并可限制：

- 指定 Agent。
- 运行或只读管理权限。
- 可调用的具体 API scope。
- 有效期和撤销状态。

## 19. Web 与飞书交付

### 19.1 Web Chat

终端用户 Web Chat 与管理后台分开部署和授权，只提供：

- 品牌和 Agent 介绍。
- 对话、附件和流式输出。
- 后台任务状态和子任务概览。
- 必要的用户审批。
- 产物下载。
- 查看和导出本人会话，查看、更正或删除本人私有记忆，以及提交本人数据删除请求。

Web Chat 不展示模型密钥、内部系统提示词、工作空间审计和管理配置。

当 Session 队首 Run 处于暂停或等待时，Web Chat 必须显示阻塞原因、待办操作、后续消息排队位置以及“新建会话”入口，不能只显示消息已发送。

“新建会话”的语义见 §19.4：它不创建新的 Session 实体，而是在同一个 Session 内划线。

### 19.2 飞书

首版支持：

- 文本消息。
- 基本附件。
- 流式能力受渠道限制时的进度更新。
- 任务完成通知。
- 必要的审批卡片或跳转管理页面。
- 飞书用户到 `ExternalIdentity` 的稳定映射。

私有部署计划默认使用飞书官方 SDK 的 WebSocket 长连接模式接收事件，不要求企业开放公网入站地址。平台同时支持 Webhook 模式；Webhook 部署必须验证签名和加密内容，并由反向代理承担 TLS、限流、请求大小限制和异常监测。M1 技术验证必须确认断线期间事件的补投语义；如果无法保证补投且企业不接受事件丢失，Webhook 在 M3 生产部署中就是必需兜底，不再定义为可选。

接入方式依据[飞书官方 Python SDK Channel Quickstart](https://github.com/larksuite/oapi-sdk-python/blob/8d6402635d0a9314ddae765ae64931aabca30f79/doc/channel/quickstart.md)；该文档同时说明 WebSocket 和 Webhook transport。

两种模式都使用 `channel_binding_id + channel_event_id` 去重，并把消息转换为统一 Run。平台不提供默认内网穿透服务；选择 Webhook 的企业负责域名、公网入口和证书。

飞书中 Session 被暂停或等待的 head Run 阻塞时，适配器发送包含原因、排队位置和当前可用审批、继续、取消或新建会话入口的状态卡片，不能静默吞入新消息。

首版的“新建会话入口”是卡片文案中写明的 `/new` 命令，不是可点击按钮：本版不渲染交互按钮，做按钮需要 card-action 回调、验签和幂等去重。卡片上那句话即入口本身，删掉它本条就没有实现。

### 19.3 渠道适配接口

渠道适配器只负责：

- 验证渠道请求。
- 解析外部用户和会话。
- 转换消息与附件。
- 发送进度、结果和审批通知。

渠道适配器不能复制 Agent Loop、权限规则和记忆逻辑。

### 19.4 聊天内命令

渠道里的最终用户可以用两条命令自行退出一段走不下去的对话，而不必联系管理员或改数据库。

- **`/undo [N]`**：收回最近 N 轮（默认 1，`N` 超过实际轮数则收回到最早一轮，不报错）。
- **`/new`**（别名 `/reset`）：收回整段历史，开始一段干净的对话。

**只有整条消息精确匹配才是命令。** 其它以 `/` 开头的消息一律原样交给模型——渠道里一条路径、一个日期或一句玩笑以 `/` 开头很常见，把它们当命令吞掉会让消息看起来石沉大海，比命令不可用更糟。带附件的消息不是命令。

**收回不是删除。** 被收回的消息保留在库中并出现在转写记录里、标记为已撤回，但不再进入模型上下文、不作为子 Agent Run 的结果、不被复制进检查点、不参与会话搜索（§14.3）、也不会作为出站回复发出。最后一条尤其重要：否则渠道与模型上下文会讲两个不同的故事——用户收到一条答复，而模型的历史里没有它。

这与 §344 的擦除是两件事：擦除等于不存在，收回是「用户收回了」。

**`/new` 的语义是在同一个 Session 内划线，不创建新的 Session 实体。** 预算、排队和记忆快照都挂在 Session 上，`/new` 并不重置它们；这一点必须在文档中说明，不能让用户以为得到了全新的额度。选择原地划线而非轮转 Session id，是为了不产生任何入口都够不着的孤儿 Session。

**未了结的工作。** `/undo` 在 Session 存在任何非终态 Run 时一律拒绝，并说明是在跑还是在排队。`/new` 区别对待：队首 Run 停靠在等待审批、等待外部或暂停状态时允许执行，并一并结束该 Session 所有未了结的 Run；只要有 Run 正在跑或处于不可取消的状态，则拒绝且一条消息都不收回。

这个不对称是有意的：`/new` 是逃生出口，被卡住的人正是在队首停靠时最需要它——若此时拒绝，§19.2 状态卡片上写的入口就是一句空话；而 `/undo` 是对已了结历史的外科操作，没有理由替用户放弃一个他没要求放弃的 Run。停靠的 Run 可以安全取消，是因为它没有任何工具在执行——它在等人或等外部事件，取消不会把已经发生的副作用切成两半。

**取消是全有或全无**：动手前先检查全部未了结 Run 是否都可取消，任一不可取消则一个都不取消。这一层没有回滚，挡住「取消一半」的是那道前置检查，不是异常处理。

命令不产生 Run，因此不进队列、不计预算、不写入历史。执行结果必须回执给发送者，说明收回了多少轮、多少条、结束了多少个 Run，并回显被收回的那条消息原文，方便用户改后重发。

## 20. UI 设计

### 20.1 管理后台导航与阶段边界

M1 交付同一 `web` 应用的最小控制台，只覆盖工作空间创建与成员基础管理、Agent 草稿与发布、Runs 列表与基础详情、Playground。M3 在此应用上补齐完整管理后台，包括 Approvals 待审队列、Usage、Audit Logs 查询导出和完整父子任务树；独立 `chat-web` 也在 M3 交付。

M3 完整导航为：

- 概览。
- Agents。
- Runs。
- Approvals。
- Skills。
- Tools & MCP。
- Channels。
- Members。
- Usage。
- Audit Logs。
- Settings。

工作空间切换器固定在全局导航顶部。

### 20.2 Agent Builder

包含：

- 基本信息。
- 人格。
- 模型。
- 记忆。
- 技能。
- 工具。
- 子 Agent。
- 安全策略。
- 渠道。

简单模式提供向导、模板和推荐默认值；高级模式提供 YAML/Markdown 编辑、导入和导出。

右侧提供实时 Playground 和事件轨迹。底部固定提供保存草稿、查看差异和发布新版本。

M1 Agent Builder 只实现基本信息、人格、模型、工具与安全阀的必需字段；记忆、技能、子 Agent 和渠道界面随所属里程碑增加。

### 20.3 Run Detail

包含：

- 概要。
- 时间线。
- 父子任务树。
- 上下文和压缩事件。
- 产物。
- Token 和费用。

允许有权限的用户暂停、继续和取消。对 `failed` Run 点击“重试安全步骤”时创建带 `retry_of_run_id` 的新 Run，原 Run 仍为终态；对可安全恢复的 `interrupted` Run 则使用状态矩阵中的 `interrupted → queued`。

### 20.4 设计原则

- 管理后台是运行控制台，不是放大的聊天页面。
- 默认界面只展示完成当前任务所需的信息，高级配置逐步展开。
- 草稿、已发布和已停用状态必须清楚区分。
- 危险操作展示影响范围并要求明确确认。
- 支持浅色、深色和键盘操作，满足常见无障碍要求。

## 21. 技术栈

### 21.1 总体形态

采用“模块化单体代码库 + 独立运行进程”：

- API、Worker、Scheduler 和 Web 可以独立启动和扩容。
- 后端业务模块共享同一套 Python 包和数据库模型。
- 首版不拆成大量微服务。

### 21.2 后端

- Python。
- FastAPI。
- Pydantic。
- SQLAlchemy 2。
- Alembic。
- PostgreSQL。
- Redis。
- MinIO/S3。

模型提供方使用 OpenAI、Anthropic 和 Gemini 官方 SDK，通过内部 `ModelProvider` 接口统一。

OpenAI 兼容端点是一等 ModelEndpoint 类型，管理界面和 API 明确配置 `base_url`、`api_key_secret_id`、`model_name`、输入上下文窗口、最大输出、`context_accounting`、`usage_quality`、其他能力声明和可选定价。它用于 vLLM、Ollama、DeepSeek、Qwen 和企业模型网关等兼容服务，不能只隐含在 OpenAI SDK 的默认配置中。

MCP 使用官方 Python SDK。首版不引入 LangChain 作为核心抽象层，避免 Agent 内核被大型框架接口绑死。

### 21.3 Run 调度

PostgreSQL 中的 Run 是唯一可信状态。Worker 使用 WorkerLease 获取某个 Session head Run 的一个执行时间片：

- 领取时记录 Worker、租约期限和尝试次数。
- Worker 定期续租。
- 租约过期后，只允许安全任务重新领取。
- Redis 用于立即唤醒 Worker；Redis 消息丢失后，Worker 仍能从数据库发现待执行 Run。

sandbox-controller 的每个操作都必须携带适用的 `run_id` 和 `sandbox_id` 并校验 SandboxReservation 归属。Worker 发起的 `acquire`、`execute`、`inspect`、`freeze`、`thaw` 或 `destroy` 都必须校验该 Run 当前持有匹配且未过期的 WorkerLease。租约过期后的 Scheduler inspect/freeze/destroy 使用独立、可审计的清理权限，仍必须匹配 Run 与 SandboxReservation，不能借此操作其他 Run 的沙箱。

一个 Worker 在 WorkerLease 期间可以连续执行多个检查点步。对异步 Run，一个 Goal 轮结束或 `max_slice_seconds` 到期后，在下一个安全检查点提交 SessionWorkspace，冻结 SandboxInstance，再把 Run 转回 `queued` 并释放 WorkerLease；同一 Worker 可以立即重新竞争该 Run，但不得跨时间片私有占用。SandboxReservation 与已冻结 SandboxInstance 可在 `sandbox_idle_ttl` 内保留，新 WorkerLease 获取后再解冻，因此重新调度不等于沙箱冷启动。Chat Completions 同步 Run 在 §18.1 的同步交付窗口内不做普通轮间切换，直到完成、等待、暂停、取消或达到同步时限。

异步 Run 的 `max_slice_seconds` 实例默认为 30 秒，平台管理员可在 10–300 秒之间调整，工作空间只能调低。`sandbox_idle_ttl` 实例默认为 5 分钟，平台管理员可在 0–30 分钟之间调整，工作空间只能调低。TTL 为 0 表示每个执行时间片后销毁沙箱，但不影响同一时间片内的多个工具调用。

独立 `scheduler` 进程负责：

- 发现过期 WorkerLease，先把关联 SandboxReservation 标记为 `quarantined` 并要求 sandbox-controller 冻结或销毁 SandboxInstance，再按状态矩阵把 `running` 或 `cancelling` 转为 `interrupted`。只有沙箱已停止、工作区版本已确认且当前检查点可安全重放时，才把 Run 从 `interrupted` 放回 `queued`。
- 巡检 Session 队首不变量：`head_run_id` 指向终态或不存在的 Run、`head_run_id` 为空但存在非终态 Run、head 不是最小非终态 Run，或 pending Run 的 `blocked_by_run_id` 不正确时，锁定 Session 并按 §11.1 重算队首与阻塞关系。修复后写入 `session_head_repaired` 系统审计事件；存在新 head 时同时写入该 Run 的同名 RunEvent，再唤醒 Worker。
- 处理 `wait_deadline_at`、Approval `expires_at` 和 `paused(compat_timeout)` 24 小时清理。
- 回收超过 `sandbox_idle_ttl` 的 SandboxReservation 和 SandboxInstance，销毁前确认 SessionWorkspace 已提交。
- 清理到期 `IdempotencyRecord` 和 `ChannelEventReceipt`。
- 执行保留期删除、`DataDeletionJob` 和其他可恢复后台任务。

多个 scheduler 实例同时存在时，使用 PostgreSQL advisory lock 选出当前扫描者，单个任务再用行锁和状态版本领取，使重复扫描不会重复执行副作用。领导实例失联后，其他实例可获得 advisory lock 继续处理。

首版不把 Celery、Redis 或其他队列自身的状态当作业务真相。

### 21.4 前端

- TypeScript。
- React。
- Vite。
- Ant Design。
- TanStack Query。
- Monaco Editor 用于 YAML/Markdown 高级编辑。
- Playwright 用于端到端测试。

### 21.5 可观测性

- 结构化日志。
- OpenTelemetry trace。
- Prometheus 指标。
- RunEvent 业务事件。

日志和 trace 不能替代审计记录。审计记录属于业务数据，具有权限和保留策略。

### 21.6 建议代码结构

```text
apps/
  api/
  worker/
  scheduler/
  web/
  chat-web/

packages/
  agent-core/
  control-plane/
  run-engine/
  memory/
  skills/
  tools/
  sandbox/
  channels/
  providers/
```

每个模块必须有明确的公开接口，不能依赖其他模块的内部数据库查询或私有实现。

## 22. 部署

Docker Compose 首版包含：

- `api`
- `worker`
- `scheduler`
- `web`
- `chat-web`
- `sandbox-controller`
- `egress-proxy`
- `postgres`
- `redis`
- `minio`

此清单是 0.3 完整 Compose 形态。M1 必须启动 `api`、`worker`、`scheduler`、最小 `web`、`sandbox-controller`、`postgres`、`redis` 和 `minio`，可信进程使用内置 `SafeOutboundClient`；M2 在交付联网工具前加入 `egress-proxy`，`chat-web` 在 M3 加入。生产 profile 额外包含 `gateway`，默认使用 Caddy 作为反向代理和 TLS 终止点。企业可以替换为现有网关，但必须传递可信代理头并限制管理 API 的暴露范围。

初始化向导负责：

- 创建首个平台管理员。
- 配置本地登录或 OIDC。
- 配置至少一个模型别名。
- 创建首个工作空间。
- 创建或导入示例 Agent。

提供健康检查、数据库迁移、备份、恢复和升级回滚说明。

本地开发允许仅绑定 loopback 的 HTTP。非 loopback 的 Web、API、OIDC 回调和飞书 Webhook 必须使用 HTTPS；证书由企业提供或通过 ACME 获取。飞书 WebSocket 长连接不需要公网入站 TLS，但其出站连接仍受平台网络策略和企业防火墙控制。

## 23. 安全要求

必须通过自动化测试证明：

| ID | 首次必须通过 | 可证伪断言 |
|---:|---|---|
| 1 | M1 | 使用工作空间 A 的身份请求工作空间 B 的已知资源 ID 时，服务端返回拒绝结果且不会返回资源字段。 |
| 2 | M1/M2 | 使用终端用户 A 的身份请求用户 B 的私有会话或文件时，服务端拒绝并写安全审计事件；M2 引入私有记忆后，对私有记忆执行同样测试。 |
| 3 | M2 | 子 Agent 请求未出现在父权限与 `delegation_scope` 交集中的工具、文件、网络、密钥、技能或记忆能力时，执行层拒绝并写 RunEvent。 |
| 4 | M1 | 未出现在 ToolBinding 中的工具即使被模型输出为合法 `tool_call`，执行层仍返回 `tool_not_authorized`，不能调用底层实现。 |
| 5 | M1 | 文件工具使用绝对路径、`..`、符号链接或绑定挂载尝试访问允许工作区之外的路径时，sandbox-controller 拒绝。 |
| 6 | M2 | 没有出网白名单的沙箱访问公网或内网保留地址时连接失败；配置白名单后只允许命中的域名与解析地址。 |
| 7 | M1/M2 | Secret 值经过提示词、日志和 RunEvent 序列化路径时均被拦截或脱敏；M2 引入 MemoryItem 和 SkillProposal 后，对新增路径执行同样测试。 |
| 8 | M2 | 审批后的工具名、参数、工作目录、目标资源或权限任一发生变化时，执行层返回 `approval_mismatch`。 |
| 9 | M1/M2 | 对已发布 AgentVersion 发送更新请求时服务端拒绝；M2 引入 SkillVersion 后，对它执行同样测试。修改只能产生新版本。 |
| 10 | M1/M2 | 终端用户清除任务完成后，使用原 ID 不能查询私有会话和文件；M2 引入私有记忆后，对记忆执行同样测试。清除记录只包含脱敏统计。 |
| 11 | M1 | 只取得数据库备份而没有部署侧 KEK 时，无法解密 Secret；KEK 轮换中断后能够从审计记录的最后进度继续。 |
| 12 | M1 | API 或 Worker 把 `ModelEndpoint.base_url` 或渠道技术验证目标传给 `SafeOutboundClient` 时，loopback、link-local、云元数据地址和未批准私有网段被拒绝；重定向和 DNS rebinding 不得绕过检查。 |
| 13 | M2 | HTTP/OpenAPI、MCP、`ModelEndpoint.base_url` 或渠道连接到上述禁止目标时，`egress-proxy` 在网络边界拒绝连接；直接连接和每次重定向都使用同一校验。 |
| 14 | M1/M2 | M1 中工作空间管理员提交未经平台管理员批准的模型或渠道私有端点时配置被拒绝；M2 中 Agent/Run 请求超出平台、工作空间或 Agent 上层允许集合时，配置或连接被拒绝并写安全审计事件。 |
| 15 | M1 | 代码中出站包之外出现原始 HTTP 客户端或 socket 创建时，架构检查失败；沙箱即使绕过工具封装直接建立连接，Docker 网络策略仍使连接失败。 |

`M1/M2` 表示 M1 先验证当时已经实现的数据和执行路径，M2 新增对象或能力时再扩充同一断言；不表示可以把 M1 已有路径的测试推迟到 M2。

这些要求不能用“已经使用 Docker”代替。正式发布前还需要独立的威胁建模和安全评审。

## 24. 可靠性要求

- API 或 Worker 重启后，会话和已保存 Run 状态仍存在。
- SSE 支持从事件序号恢复。
- 未产生副作用的步骤可以重新调度。
- 无法确认结果的写操作进入人工处理。
- 子 Agent 结果投递失败后继续保留和重试。
- 上下文压缩失败不丢原文。
- 达到安全阀后保存现场并暂停。
- 所有后台任务均有取消路径。
- 运行中的代码无法精确恢复时，平台明确标记 `interrupted`，不能伪装成仍在运行。

### 24.1 M1 可量化基线

M1 使用统一参考环境验收：Linux、8 vCPU、16 GB RAM、本地 SSD，API、Worker、Scheduler、sandbox-controller、PostgreSQL、Redis 和 MinIO 运行在同一 Docker 主机；模型替身固定延迟 50 ms，沙箱镜像已经下载，数据库预置 10 万条 RunEvent。指标只计算 tiny-hermes 平台开销，不把真实模型网络延迟算入。

| 指标 | M1 通过门槛 |
|---|---:|
| 创建 Run | 在 200 个不同 Session 上均匀分布，连续 5 分钟 20 请求/秒，P95 不高于 300 ms，错误率低于 0.1%；单 Session FIFO 另做正确性测试，不混入此吞吐基准 |
| 写入 RunEvent | 连续 5 分钟 500 条/秒，P95 不高于 100 ms，已提交事件零丢失 |
| SSE | 同时保持 500 个连接 10 分钟，每 5 秒接收事件，重连后无已提交事件缺口 |
| Docker 沙箱冷启动 | 镜像已缓存时 P95 不高于 3 秒 |
| 保温沙箱重新获取 | SandboxInstance 仍在 `sandbox_idle_ttl` 内时 P95 不高于 300 ms，且容器 ID 不变 |
| SessionWorkspace 增量提交 | 修改 1 个 1 MiB 文件并提交到本地 MinIO 时 P95 不高于 1 秒，已确认检查点零丢失 |
| SessionWorkspace 大目录提交 | 修改 1,000 个文件、总计 100 MiB 并提交到本地 MinIO 时 P95 不高于 15 秒，已确认检查点零丢失 |
| 同 Session 连续 Run 首步延迟 | 平台运行时镜像层已在 Docker 主机缓存、SessionWorkspace 为 1 MiB 时，前一 Run 结束到后一 Run 的首个工具可用 P95 不高于 3 秒；两个 Run 使用相同只读镜像层和不同可写层。Agent 专属依赖不属于该缓存前提 |
| Worker 故障恢复 | 杀死 Worker 后，可安全重试的 Run 在 30 秒内重新进入 `queued` |
| 服务恢复 | API 或 Worker 重启后 60 秒内恢复健康检查和新任务处理 |

这些是 0.1 技术预览版的工程门槛，不代表所有企业规模。调整数值必须通过设计变更记录，不能在测试失败后直接降低门槛。

## 25. 测试策略

### 25.1 单元测试

- Agent Loop。
- Goal 判断和安全阀。
- 累计 Worker 执行时间与 24 小时墙钟期限分别计算，排队时间不能消耗执行预算。
- Run 权威状态矩阵、暂停安全点和非法转换拒绝。
- 检查点步持久化、异步执行时间片释放 WorkerLease、同步 Chat 交付窗口不做普通轮间切换、沙箱保温和 scheduler 超时回收。
- 依赖数据库唯一约束的并发入口幂等、Session head/pending FIFO、队首交接跳过终态 Run、Runs API 成功创建 pending Run 与 Chat Completions 409 两种 `session_blocked` 响应，以及渠道事件去重。至少覆盖 Run1 执行、Run2 已取消、Run3 排队、Run1 完成后 Run3 成为新 head 的场景。
- API、Worker 和 scheduler 并发写 RunEvent 时通过 `next_event_sequence` 原子分配连续序号，唯一约束冲突不会丢事件。
- scheduler 对队首指向终态或不存在的 Run、队首为空但仍有非终态 Run、队首不是最小非终态 Run 和错误 `blocked_by_run_id` 的修复。
- Chat Completions 默认创建一次性 Session、显式 Header 复用持久 Session、拒绝用 API Key 或 `user` 字段自动映射，以及持久 Session 阻塞时返回 409 且不创建 Run。
- 固定角色权限矩阵和工作空间范围。
- 多工作空间用户必须用 `X-Workspace-Id` 选择并通过 Membership 校验；API Key 从绑定推导且不能用 Header 越权切换。
- 记忆作用域。
- Agent 与技能版本规则。
- 配置 Schema 迁移和旧 AgentVersion 回滚。
- 上下文预算调整权限、缩放建议、压缩失败和 `paused(context_overflow)`。
- 工具预算超限事件与 `paused(tool_budget_exceeded)`。
- 审批类型、交互主体、无交互预授权、TTL、参数哈希与权限。
- 根 Run、重试链和后续任务树在 `budget_root_run_id` 下共享安全阀；并发派生重试时默认最多只有 3 个成功，并只消费根预算剩余额度。

### 25.2 集成测试

- PostgreSQL 与迁移。
- MinIO/S3。
- Redis 唤醒丢失后的数据库恢复。
- CanonicalMessage 与 OpenAI、Anthropic、Gemini 适配器互转。
- Provider 切换边界、usage 和 ModelPricingVersion。
- `usage_quality` 的 provider、estimated 和 unavailable 三种降级路径。
- MCP 和 OpenAPI 工具。
- MCP 运行时 schema 变化和工具预算超限。
- Docker 沙箱策略。
- M1 `SafeOutboundClient` 与 M2 `egress-proxy` 对 API、Worker 和沙箱出站目标的分阶段拒绝策略。
- `/workspace/data` 的 SessionWorkspace revision/CAS 提交和冲突处理，`/workspace/cache` 与 `/tmp` 不进入持久 revision，沙箱保温恢复，以及父子 Artifact 授权传递。
- 同一 Run 保温时可复用可写层；同 Session 的连续 Run 只能复用平台运行时 Docker 镜像只读层，必须得到不同可写层。冷启动返回 `cache_state=reset` 并在下一轮模型调用前产生结构化提示。
- sandbox-controller 在每次操作前校验 Run、SandboxReservation 和适用的 WorkerLease 所有权；Scheduler 过期清理使用单独权限。
- Secret 信封加密、KEK 轮换中断与恢复。

### 25.3 端到端测试

- 创建、测试、发布和回滚 Agent。
- Web Chat 完成一次对话任务。
- 飞书完成一次任务并接收结果。
- API 创建长 Run 并订阅 SSE。
- Chat Completions 遇到等待审批或同步超时时返回明确 Runs API 引导。
- Chat Completions 默认请求分别创建不同的一次性 Session，显式 `X-Tiny-Hermes-Session-Id` 才复用持久 Session；持久 Session 已阻塞时返回 409 `session_blocked`，且不创建 pending Run。
- Session 被暂停或等待的 head Run 阻塞时，各入口返回原因、排队位置和可用操作。
- 主 Agent 并行委派子 Agent。
- 工具审批与参数变化失效。
- Agent 提出并发布新技能版本。

### 25.4 安全与故障测试

- 跨工作空间越权。
- SSRF。
- 路径逃逸。
- 命令注入。
- 密钥泄漏。
- 模型超时和限流。
- Worker 崩溃。
- Redis 消息丢失。
- SSE 断线。
- SSE 游标早于保留期时返回 410 和 Run 快照。
- Session 队首记录损坏后由 scheduler 恢复到最小非终态 Run，已取消的 pending Run 不会成为新队首。
- 数据库短暂不可用。

### 25.5 性能测试

首版必须提供与 §24.1 相同硬件、模型替身、数据集和任务的可重复基准脚本，输出原始数据、P50/P95/P99、错误率、CPU、内存和测试版本，并公开实测结果。不能用真实模型的波动掩盖平台性能，也不能把参考环境结果外推为无限扩容承诺。

## 26. 首版内部里程碑

### M1 / 0.1 Technical Preview：安全运行骨架

- 工作空间、成员和固定角色。
- 本地账号、AuthIdentity 提供方抽象和 API Key scope。
- AuditEvent 数据模型及所有管理写操作的审计写入。
- Secret 信封加密、KEK 标识和可恢复轮换基础流程。
- Agent 配置与发布版本。
- Session head/pending FIFO、SessionWorkspace `/workspace/data` revision、可重建 `/workspace/cache`、平台运行时 Docker 镜像只读层、cache 重置信号、Run 状态矩阵、检查点步、执行时间片、入口幂等、事件流、暂停和取消。
- 独立 scheduler，覆盖租约过期、等待超时、兼容超时清理、幂等和保留期任务。
- Provider 中立消息格式、OpenAI 兼容端点、`usage_quality` 降级和 Token 统计。
- 单 Agent 文件与命令工具循环，不交付通用联网工具。
- PostgreSQL、对象存储、强制 Docker 沙箱和统一 `SafeOutboundClient`；M1 必须包含其目标校验与禁止旁路的架构测试。
- Chat、Runs API 和最小控制台：工作空间与成员基础管理、Agent 草稿与发布、Runs 列表与基础详情、Playground。
- 完成一次飞书 WebSocket 长连接技术验证，记录支持的应用类型和事件类型、断线重连行为、断线期间事件是否补投，并根据实测结果决定生产环境是否必须用 Webhook 兜底。该调查不作为 0.1 产品功能，也不在验证前预设补投结论。

M1 可以独立发布为 0.1 技术预览版，但只能描述为“单 Agent 安全运行骨架”，不能宣传为企业级多 Agent 平台。

### M2 / 0.2 Agent Preview：轻量 Hermes 核心

- 自动 Goal 循环。
- 一层并行子 Agent。
- 双层记忆和会话搜索。
- 上下文压缩与恢复。
- 技能草稿、扫描、审批、版本和回滚。
- 独立 `egress-proxy`，上线后再交付 MCP、OpenAPI/HTTP 工具、两类人工审批和费用安全阀。

M2 可以独立发布为 0.2 Agent 预览版，用于验证自主任务和多 Agent 体验。

### M3 / 0.3 Enterprise Preview：企业交付闭环

- OIDC 实现、审计查询与导出、治理审批队列。
- 完整管理后台（Approvals、Usage、Audit 查询导出、完整任务树）和独立终端用户 `chat-web`。
- 飞书 WebSocket 适配器和按 M1 技术验证结果决定的 Webhook 生产兜底。
- 用量、安全阀和任务树管理。
- 备份恢复、升级文档和安全测试。
- Apache-2.0 许可证、贡献指南、安全报告流程和示例 Agent。

只有 M1、M2 和 M3 全部完成，才称为 0.3 企业预览版。每个里程碑都必须独立发布、收集真实反馈并重新评估后一里程碑范围，不要求在没有用户验证的情况下连续完成三个里程碑。

## 27. 分阶段验收场景

### 27.1 M1 / 0.1 Technical Preview

在一套全新的 Docker 环境中，评审者能够：

1. 启动平台，通过本地账号完成初始化并创建两个工作空间。
2. 创建 Agent 草稿，在 Playground 测试并发布不可变版本。
3. 通过 Chat Completions 和 Runs API 执行单 Agent 任务并从 SSE 读取连续事件；连续两次默认 Chat 请求得到不同的 ephemeral Session，显式传入 `X-Tiny-Hermes-Session-Id` 时复用同一 persistent Session，该 Session 已阻塞时返回 409 `session_blocked` 且不创建新 Run。
4. 对同一 Run 创建请求重复使用 Idempotency-Key，确认只产生一个 Run。
5. 通过 Runs API 或 Playground 在同一 Session 连续发送三条消息，确认只有 head Run 可被领取且后续 Run 按 FIFO 执行；取消第二个 pending Run 后完成第一个，确认第三个 Run 成为新 head。head Run 暂停时再发消息，确认返回 `session_blocked`、原因、排队位置和可用控制操作；人为制造错误 `head_run_id` 后，确认 scheduler 能修复并写 `session_head_repaired`。
6. 人工暂停、继续和取消 Run，并验证状态转换矩阵拒绝非法转换。
7. 在强制 Docker 沙箱中连续执行安装依赖、写文件和运行脚本等多个工具步骤，确认同一执行时间片不重建沙箱，保温重新获取不改变容器 ID，`/workspace/data` 已提交变更可恢复；冷启动返回 `cache_state=reset` 并在下一轮向模型明确提示缓存已重置。同 Session 的下一 Run 只复用平台运行时 Docker 镜像只读层并获得新可写层，Agent 依赖按锁文件在新 cache 重建，沙箱失败不会回退宿主机。
8. 重启 API、Worker 和 Scheduler，确认会话、审计、事件和可安全重试的 Run 能够恢复，过期租约不会被重复回收；API、Worker 与 scheduler 并发写事件时序号仍唯一连续。
9. 使用另一个工作空间身份请求已知资源 ID，确认服务端拒绝且不返回资源数据；多工作空间用户缺少 `X-Workspace-Id` 时得到明确错误，API Key 不能用该 Header 切换到其他工作空间。
10. 把模型端点配置为 loopback、云元数据地址、未批准私有网段和跳转到这些地址的 URL，确认 `SafeOutboundClient` 全部拒绝；向出站包之外引入原始 HTTP 客户端时架构检查失败。
11. 交付飞书长连接技术验证记录，明确标记已实测事实、尚未确认项和 Webhook 兜底决策。
12. 从 `failed` Run 连续及并发派生重试，确认所有重试使用同一 `budget_root_run_id`、只能消费剩余预算且默认最多 3 个成功；Run Detail 能显示来源并提供或禁用“重试安全步骤”。
13. 运行 §24.1 基准，全部达到 0.1 门槛。

M1 发布后必须收集安装成功率、失败 Run、沙箱启动、API 使用和开发者反馈，再决定是否调整 M2 计划。

### 27.2 M2 / 0.2 Agent Preview

1. Agent 使用 Goal 判断完成、继续和等待，并在安全阀触发后进入可恢复暂停。
2. 主 Agent 并行委派至少两个子 Agent，父子文件只通过 Artifact 授权传递，整棵任务树与重试链共享根预算。
3. 子 Agent 不能读取未授权的父 Agent 私有记忆，也不能扩大工具、网络、密钥或技能权限。
4. 用户私有记忆和 Agent 共享记忆按不同审批策略写入。
5. Agent 提出技能变更，经扫描、差异、审批和发布后形成不可变新版本。
6. 高风险工具进入正确审批队列，终端用户不能批准治理操作，参数变化使原审批失效；无 EndUser 的 ServiceAccount Run 只能使用发布时已批准的预授权或治理审批。
7. 上下文压缩保留原始引用，压缩失败且无法装入窗口时进入 `paused(context_overflow)`。
8. 独立 `egress-proxy` 在 MCP 和 OpenAPI/HTTP 工具之前上线，强制执行平台、工作空间、Agent 和 Run 出站范围的交集。
9. MCP 和 OpenAPI 工具通过两次权限检查执行；MCP schema 超预算时写入 `tool_schema_budget_exceeded` 并进入 `paused(tool_budget_exceeded)`。

M2 发布后必须评估 Goal 失控率、子 Agent 成功率、审批负担、记忆误写和技能提案质量，再决定 M3 产品范围。

### 27.3 M3 / 0.3 Enterprise Preview

1. 通过 OIDC 登录并按固定角色访问工作空间资源。
2. 从终端用户 Web Chat、飞书 WebSocket 和 Runs API 调用同一个 AgentVersion。
3. 飞书重复投递同一事件时只创建一个 Run；如 M1 技术验证判定 Webhook 是生产必需兜底，则必须通过 HTTPS 接收并验证请求。
4. 管理员查看 Approvals、任务树、用量、错误和脱敏审计记录。
5. 执行 Secret KEK 轮换，在中断后恢复并完成全部重包。
6. 完成备份、恢复和升级回滚演练。
7. 通过完整安全测试、许可证检查和开源发布材料验收。

## 28. 开源与许可证要求

- 项目许可证采用 Apache-2.0 方向。
- 不复制 FastClaw 受附加条款约束的源码或前端资源。
- 如复用 Hermes Agent 的 MIT 代码，保留原始版权与许可证声明，并记录复用文件清单。
- 第三方依赖在发布前生成许可证清单。
- 仓库包含贡献指南、安全漏洞报告流程、行为准则和架构说明。
- 正式发布前对许可证结论进行法律审查；本文不构成法律意见。

## 29. 后续版本方向

首版稳定后，再按真实使用数据评估：

- Kubernetes 和多副本。
- 企业微信、钉钉及更多渠道。
- 两层以上 Agent 编排和受管工作树。
- 公共技能市场。
- 自动后台技能学习和 Curator。
- 高级记忆、Dreaming 和知识库。
- 云沙箱和远程执行节点。
- 按 AgentVersion 锁文件构建、签名、缓存和淘汰的依赖只读层；在单独安全设计完成前不进入 M1。
- ShareGPT/RL 轨迹导出、评估和训练数据管理。
- 公有 SaaS 组织、计费和套餐。

这些方向不属于首版验收条件，也不应在首版实施中提前扩张。

## 30. 后续实施计划边界

本文是产品级总设计，不是一份可以在单个开发阶段内完整执行的实施计划。

- M1、M2 和 M3 分别建立独立技术设计、任务计划和验收记录。
- 第一份实施计划只覆盖 M1“安全运行骨架”。
- M1 验收通过后，才为 M2 编写详细计划；M2 通过后再进入 M3。
- 后一里程碑可以使用前一里程碑已经验证的接口，但不能提前把后一里程碑功能混入当前范围。
- 本文中的未来版本方向不进入 M1、M2、M3 的隐含任务。

这种分解保留 0.3 企业预览版的目标，但 M1 和 M2 都是可独立发布、可被用户验证的产品版本。M1 或 M2 的真实反馈可以通过新的设计变更调整后续范围，不能把 v2.4 文档当作不可修改的九个月固定清单。

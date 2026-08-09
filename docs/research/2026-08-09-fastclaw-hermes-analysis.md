# FastClaw 与 Hermes Agent 一手资料研究

> 调研日期：2026-08-09  
> FastClaw 核对版本：`dev` 分支 [`533a138`](https://github.com/fastclaw-ai/fastclaw/tree/533a138236b82ede46ecdfb9dc09b3087d3b17d4)，提交时间 2026-07-27  
> Hermes Agent 核对版本：[`3f83297`](https://github.com/NousResearch/hermes-agent/tree/3f832978d30e0e14437edbf7a3f63315f08bad36)，提交时间 2026-08-09  
> 资料范围：两个官方仓库的 README、官方文档、部署清单和源码。本文不把宣传语当作已经验证的生产能力。

## 结论先行

1. **FastClaw 更接近“多用户 Agent 平台骨架”**：有用户与 Agent 所有权、三类 API Key、上游最终用户映射、管理后台、用量与配额接口、PostgreSQL/对象存储/Redis 多副本方案。它适合参考控制面、数据分区和上游 API 的设计。
2. **Hermes Agent 更接近“功能丰富的个人/单实例 Agent Runtime”**：Agent 循环、人格、记忆、技能、工具、会话搜索和多种执行环境已经很完整；`profile` 可以运行多个独立 Agent，但官方明确说 profile 不是沙箱。它适合提炼轻量 Agent 内核，不宜整体搬进首版平台。
3. **两者都不能直接等同于完整的企业多租户平台**。FastClaw 主要是用户所有权模型，没有在核对资料中看到完整的 `Organization → Member → Role → Project` 企业组织模型；Hermes 的 profile 主要是本机状态目录边界，Kanban 的 tenant 也被官方称为软过滤条件。
4. **FastClaw 不能被当作普通开源底座直接改造**。它官方称自己为 source-available，许可证限制未经授权向多个无关组织提供 FastClaw 多租户服务，也限制移除或修改前端品牌。若目标是一个无此类附加限制的开源项目，应独立实现，只借鉴思想；直接复用源码前必须做许可证审查。
5. 推荐的组合不是“给 Hermes 套一层 FastClaw UI”，而是：**独立的多租户控制面 + 精简 Hermes 风格的 Agent 内核 + 强制沙箱执行面**。控制面负责租户、权限、配置、密钥、审计、配额与调度；Agent 内核只负责提示词拼装、模型调用、工具循环、记忆和技能加载；沙箱作为不可信代码的强边界。

下文中的“事实”都能追溯到官方资料；“判断”是基于这些事实为目标项目作出的推断。

## 一、FastClaw

### 1. 定位与核心能力

**事实**

- FastClaw 自称“轻量 Go Agent Runtime”和“Agent Factory”，用于创建、管理和运行多个 Agent；每个 Agent 有人格、记忆、技能和工具，平台负责模型通信、工具执行、沙箱隔离和会话管理。[README：定位](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L3-L32)
- 它提供单个 Go 二进制、Web 管理后台、CLI、本地/容器/Kubernetes 部署，以及 OpenAI 兼容的运行 API。[README：功能与部署](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md)
- 它支持 OpenAI、Anthropic、Ollama、OpenRouter 等模型提供方，也支持 OpenAI 兼容接口；模型配置可以被 Agent 级设置覆盖。[README：LLM Providers](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L121-L127)
- 内置工具包括命令执行、文件读写、目录查看、网页抓取、搜索和记忆搜索；另外支持 MCP 与 JSON-RPC 子进程插件。[README：Tools & Sandbox](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L133-L139)

**需要纠正的说法**

- 官方仓库没有声称 FastClaw 是 OpenClaw fork，也没有给出这种继承关系。源码能确认的是兼容 OpenClaw 技能元数据、MEDIA 文件输出协议，并提供 OpenClaw 插件桥。[技能兼容源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/skills.go#L48-L64)、[插件桥说明](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/tools/openclaw-plugin-bridge/README.md)
- 因此，准确表述应是“FastClaw 兼容部分 OpenClaw 生态约定”，而不是已经证实的“基于 OpenClaw 开发”。

### 2. Agent 人格、记忆、技能与工具模型

**事实**

- Agent 身份使用 `SOUL.md`、`IDENTITY.md`、`AGENTS.md`、`TOOLS.md`、`BOOTSTRAP.md` 等系统文件表达。[README：Agent system files](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L308-L320)
- 对公开或共享 Agent，`SOUL.md`、`IDENTITY.md` 和技能来自 Agent 所有者；每位聊天者的 `USER.md`、`MEMORY.md`、会话数据单独分区。[README：Sharing](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L83-L88)、[上下文拼装源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/context.go#L131-L180)
- `MEMORY.md` 保存长期事实；系统可以用模型从近期对话提取事实和用户偏好再写入记忆。[记忆源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/memory.go#L18-L38)、[自动提取源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/memory.go#L291-L406)
- 技能以 `SKILL.md` 为主体，存在多层来源与覆盖；默认先提供名称和简介，需要时再加载全文，减少每轮提示词长度。[技能加载源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/skills.go#L105-L125)、[覆盖与按需加载](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/skills.go#L162-L302)
- Agent 循环支持模型工具调用、并行工具执行、调用次数限制、失败恢复、上下文压缩和子 Agent 等能力；核心循环本身已经很大。[Agent loop 源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/agent/loop.go)

**判断**

- “Agent 模板数据”和“最终聊天用户数据”分开是非常值得复用的边界：人格、身份和批准的技能可共享；用户画像、长期记忆、会话与上传文件必须按最终用户隔离。
- Markdown 很适合让人编辑人格和技能，但不宜成为平台唯一的数据模型。平台仍需要数据库中的结构化元数据、版本号、发布状态、所有者、更新时间和审计记录。

### 3. 多租户、权限与隔离

**事实**

- FastClaw 当前有 `super_admin`、`user`、`app_user` 角色：超级管理员管理平台，普通用户管理自己的资源，`app_user` 用于映射上游产品的最终用户。[账户模型源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/users/account.go#L1-L35)
- API Key 分为 `admin`、`user`、`agent` 三类。Agent Key 被限制到明确的 Agent 列表；User Key 访问所有者自己的 Agent。[API Key 模型源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/users/apikey.go#L14-L28)、[鉴权源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/auth/auth.go#L33-L125)
- 上游产品可以显式调用 `POST /v1/users`，也可在聊天请求里传 `user` 或 `X-Fastclaw-End-User`。FastClaw 以 `(API Key, external_id)` 幂等映射内部用户，用于隔离记忆、会话、用量、配额和个人偏好。[Upstream API：Identity Model](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/docs/upstream-api.md#L45-L84)
- 配置存在系统、用户、Agent、用户与 Agent 组合等层级，内层覆盖外层。[scope 源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/scope/scope.go#L1-L18)
- README 明确把“用户账号、业务计费”归给上游应用，而不是 FastClaw 本身；FastClaw 提供用量与配额接口辅助上游产品。[README：What FastClaw Stores](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L90-L119)、[Upstream API：Usage And Quotas](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/docs/upstream-api.md#L224-L308)

**判断**

- FastClaw 已具备实用的用户级数据隔离和 API Key 范围控制，但这不等同于完整企业租户模型。在核对的一手资料里没有看到成熟的“组织、成员、租户管理员、项目角色、服务账号、资源策略”完整模型。
- 请求中的 `params.tenant_id` 只是本轮传给 Agent 的结构化上下文，官方说明它不持久化，不能当作正式租户安全边界。[Upstream API：Chat 扩展字段](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/docs/upstream-api.md#L115-L139)
- 目标项目应从第一天就让关键数据带上 `tenant_id`，并在服务端根据身份解析它，不能信任客户端随意传入的租户字段。

### 4. API 与 Web 管理

**事实**

- 面向上游产品的较小接口面包括：OpenAI 兼容的 `/v1/chat/completions`、`GET /v1/agents`、`POST /v1/users`、`GET /v1/usage` 和 `/v1/quota`。[Upstream API：接口选择](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/docs/upstream-api.md#L10-L23)
- Chat API 支持流式输出、显式 `agent_id`、稳定会话键、最终用户映射、图片和附件。[Upstream API：Chat API](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/docs/upstream-api.md#L86-L193)
- `/api/*` 是更宽的管理接口，用于 Agent、用户、API Key、模型、技能、渠道、会话和定时任务管理；Web 后台提供相应页面。[README：Dashboard 与 Agent Management](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L56-L88)
- Web 静态资源被打包进 Go 二进制，便于单文件交付。[Dockerfile](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/Dockerfile#L15-L44)

**判断**

- “公开运行 API”和“管理 API”分开非常合理。运行 API 应稳定、范围小、易接入；管理 API 可更丰富，但必须有更严格的权限、审计和幂等控制。
- OpenAI 兼容能降低接入成本，但不能承载 Agent 的全部管理语义。创建、发布、版本、技能绑定、密钥、沙箱策略和审计仍需要独立 API。

### 5. 架构与部署

**事实**

- 默认用 SQLite；多 Pod 可改为 PostgreSQL。Agent 身份文件、会话、记忆、配置等进入数据库；技能和工作区文件可放对象存储。[README：Architecture](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L90-L119)
- Redis 可用于多副本的渠道租约和消息流；对象存储用于多 Pod 技能与文件加载。[README：Configuration](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L177-L195)
- 官方提供 Docker Compose、Kubernetes 和 Helm 配置。Helm 默认给出多副本、HPA、PDB、PostgreSQL、对象存储和 E2B 沙箱选项。[Helm values](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/deploy/helm/fastclaw/values.yaml)
- 官方多 Pod 示例是两个相同网关共享 PostgreSQL 与 MinIO。Pod 失败后身份、会话、记忆和工作区持久数据仍在，但故障 Pod 上正在运行的沙箱需要重新创建。[Multi-pod 说明](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/deploy/multi-pod/README.md#L1-L9)、[故障边界](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/deploy/multi-pod/README.md#L115-L126)

**判断**

- “计算可丢、状态不可丢”是正确方向：Agent Run 与沙箱可重建；Agent 配置、记忆、会话、审批和产物必须持久化。
- 首版不必马上上 Redis、S3、HPA，但数据访问层应避免把本地路径写死，给 PostgreSQL 和对象存储留清楚接口。

### 6. 沙箱边界

**事实**

- FastClaw 支持 Docker、E2B，源码还包含 Boxlite；沙箱延迟创建、空闲回收，并从持久存储恢复工作区。[Docker executor](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/sandbox/docker_executor.go#L199-L323)、[lifecycle](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/sandbox/lifecycle.go#L19-L53)
- 托管部署会强制命令与文件工具进入沙箱；自托管默认仍允许宿主机执行，只有显式设置才获得同样的强制限制。[README：Tools & Sandbox](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L133-L139)

**判断**

- 对最终用户开放的企业平台不应允许回退到宿主机执行。沙箱必须是服务端强制策略，不是模型通过 `sandbox:true` 自愿选择。
- “运行在 Docker 中”不自动等于企业级隔离。还要定义非 root、只读根文件系统、能力删除、进程/CPU/内存/磁盘限制、出网策略、密钥注入和销毁策略。FastClaw Docker 创建参数可作为参考，但不能代替目标项目自己的威胁模型。[Docker sandbox 源码](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/internal/sandbox/docker.go#L242-L335)

### 7. 许可证边界

**事实**

- FastClaw 官方称其为 **source-available**，许可证基于 Apache 2.0 但增加了额外条件。[README：License](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/README.md#L408-L420)
- 未获商业许可时，不允许把 FastClaw 源码用于向多个无关组织提供 FastClaw-as-a-Service；使用其前端时也不得移除或修改 FastClaw 品牌。[LICENSE](https://github.com/fastclaw-ai/fastclaw/blob/533a138236b82ede46ecdfb9dc09b3087d3b17d4/LICENSE#L1-L54)

**判断**

- 可以研究其公开思想和接口形态，但若目标项目本身要成为独立的多租户 Agent 平台，直接复制或派生 FastClaw 源码存在明显许可证风险。
- 本文不是法律意见。正式开发前应让法律专业人员确认“思想借鉴、接口兼容、源码复用、前端复用”的边界。

## 二、Hermes Agent

### 1. 定位与核心能力

**事实**

- Hermes Agent 自称“self-improving AI agent”，重点是跨会话记忆、自动形成和改进技能、消息平台接入、工具调用、子 Agent 和多种执行后端。[README](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/README.md#L19-L30)
- 同一个 `AIAgent` 核心服务于 CLI、消息网关、ACP、批处理和 API Server。平台差异被放在入口层，而不是复制 Agent 内核。[Architecture](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/architecture.md)
- 核心 Agent 循环负责模型选择、提示词拼装、工具执行、重试、备用模型、回调、上下文压缩和会话持久化。[Agent Loop Internals](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/agent-loop.md)
- 仓库许可证是 MIT。[LICENSE](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/LICENSE)

**判断**

- Hermes 的长处是 Agent 内核运行能力，不是现成企业控制面。应提取清晰接口和最小循环，而不是把全部 CLI、消息渠道、桌面/TUI、训练轨迹和插件系统一起引入。
- MIT 许可比 FastClaw 更适合作为可复用实现来源，但仍应保留版权与许可证文本，并对具体复制范围做记录。

### 2. Agent 架构

**事实**

- 官方架构把入口、`AIAgent`、提示词拼装、模型提供方、工具派发、会话存储和工具后端分开。[Architecture：System Overview](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/architecture.md#system-overview)
- 会话数据使用 SQLite 与 FTS5 搜索；工具通过注册表发现和派发；终端支持 local、Docker、SSH、Singularity、Modal、Daytona、Vercel Sandbox 等后端。[Architecture：Major Subsystems](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/architecture.md#major-subsystems)
- 官方设计原则包括提示词稳定、执行过程可观察、可中断、平台无关核心、可选模块松耦合以及 profile 隔离。[Architecture：Design Principles](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/architecture.md#design-principles)

**判断**

- 最值得提炼的是六个小接口：`PromptBuilder`、`ModelProvider`、`ToolRegistry`、`MemoryStore`、`SessionStore`、`SandboxExecutor`。它们比复刻 Hermes 当前巨大的类和文件更利于测试和多租户托管。
- “一个核心、多种入口”应保留：API、Web chat 和以后增加的 IM 渠道都调用同一个运行服务，避免不同入口拥有不一致的能力与安全规则。

### 3. 人格、记忆、技能与工具模型

#### 人格

**事实**

- `SOUL.md` 是 Hermes 的主要身份文件，位于当前实例的 `HERMES_HOME`，作为系统提示词第一部分；`/personality` 是会话级覆盖。[Personality & SOUL.md](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/personality.md)
- `SOUL.md` 用于稳定身份、语气与风格；项目级命令、路径和约定放入 `AGENTS.md`。[同上：SOUL.md vs AGENTS.md](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/personality.md#soulmd-vs-agentsmd)

**判断**

- 目标平台可以把人格保存为可版本化的文本模板，但需要“草稿—发布—回滚”，不能让正在服务用户的 Agent 因编辑文件而即时、不可追踪地改变人格。

#### 记忆

**事实**

- 内置长期记忆由 `MEMORY.md` 和 `USER.md` 组成，容量受限，在会话开始时作为冻结快照注入提示词；当前会话中写入后要到新会话才进入系统提示词。[Persistent Memory](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/memory.md)
- 历史会话进入 SQLite，并通过 FTS5 按需搜索。官方将“关键事实常驻上下文”和“过去会话按需检索”分开。[Memory：Session Search](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/memory.md#session-search)
- 内存和技能自动写入可以要求审批；Hermes 还支持多个外部记忆提供方。[Memory：write approval](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/memory.md#controlling-memory-writes-write_approval)

**判断**

- 首版只需两层：少量“已确认的长期事实”与“可搜索的会话历史”。不需要同时接入知识图谱、多个向量库和八种外部记忆提供方。
- 企业场景下记忆写入应有来源、时间、作用域和删除能力；高风险租户应能关闭自动记忆或改为审批后写入。

#### 技能

**事实**

- 技能是按需加载的知识/流程文档，兼容 agentskills.io；主文件是 `SKILL.md`，可带 `references/`、`scripts/`、`templates/`、`assets/` 等目录。[Skills System](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/skills.md)
- Hermes 使用渐进加载：先在提示词中列出技能摘要，匹配任务后再加载全文和需要的参考文件。[Skills：Progressive Disclosure](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/skills.md#progressive-disclosure)
- Agent 可自行创建和修改技能；第三方技能安装带来源、内容哈希、安全扫描、信任级别和更新状态，并可要求人工审批。[Skills：Agent-Managed Skills](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/skills.md#agent-managed-skills-skill_manage-tool)、[Skills Hub security](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/skills.md#security-scanning-and---force)

**判断**

- 首版可实现“平台批准技能 + 租户私有技能 + Agent 绑定”三层，不必实现任意市场搜索、自动安装、技能自修改和复杂覆盖。
- 技能必须有不可变版本和批准状态。运行中的 Agent 应绑定特定版本，避免上游内容变化后未经审核进入生产。

#### 工具与沙箱

**事实**

- Hermes 将工具组织为 toolset，可按入口/平台开启或关闭；支持内置工具与动态 MCP 工具。[Tools & Toolsets](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/tools.md)
- Docker 后端是进程生命周期内的长驻容器，并不是每个命令新建容器；local 后端直接拥有宿主用户权限。[Tools：Docker Backend](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/tools.md#docker-backend)
- 官方安全文档覆盖危险命令审批、文件写入安全、用户授权、容器隔离、环境变量传递、SSRF 和生产部署清单。[Security](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/security.md)

**判断**

- 目标项目应把“工具授权”和“工具执行”分开。Agent 只能看见已授权的工具，真正执行前还要由服务端策略检查租户、用户、Agent、参数、审批和沙箱状态。
- 第一版只需少量高价值工具：文件读写、受限命令执行、HTTP 获取/搜索、记忆读写和 MCP 客户端；浏览器控制、电脑控制、语音、图像生成可后置。

### 4. Profiles、多 Agent 与隔离

**事实**

- Hermes profile 是独立 `HERMES_HOME`，每个 profile 有自己的配置、密钥、`SOUL.md`、记忆、会话、技能、定时任务和状态库。[Profiles](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/profiles.md)
- 官方明确说明：profile 不是工作区，也不是沙箱；local 终端后端仍拥有当前操作系统用户的文件权限。[Profiles：Profiles vs workspaces vs sandboxing](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/profiles.md#profiles-vs-workspaces-vs-sandboxing)
- 可以一个 profile 一个网关进程，也可以让默认网关复用一个进程服务多个 profile。复用模式会按 profile 路由配置、技能、记忆、人格和提供方密钥，API 前缀也使用各 profile 自己的 key。[Multi-profile gateways](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/multi-profile-gateways.md#alternative-one-gateway-for-all-profiles-multiplexing)
- profile 分发包可以包含人格、配置、技能、定时任务和 MCP 连接，但凭证、记忆与会话留在安装机器。[Profiles：Distributions](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/profiles.md#sharing-profiles-as-distributions)
- Hermes Kanban 的 tenant 是一个 board 内的可选字符串命名空间，官方明确称它为“软过滤”；board 才是硬边界，而且该功能的威胁模型是可信本机用户。[Kanban：Core concepts](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/kanban.md#core-concepts)

**判断**

- profile 是很好的“Agent 实例打包模型”，但不能直接拿 profile 名称充当企业 tenant。租户必须是数据库里受权限控制的第一等对象。
- 目标平台可以借鉴 profile distribution：将人格、默认模型、技能绑定和工具策略打成 Agent 模板；创建 Agent 时复制模板，但不复制记忆、会话和渠道凭证。

### 5. API 与 Web 管理

**事实**

- Hermes API Server 提供 OpenAI Chat Completions、Responses、Runs、Jobs、Sessions、Skills/Toolsets discovery 等接口，支持 SSE、取消运行和人工审批。[API Server](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/api-server.md)
- API 使用一个 bearer key；多 profile 复用监听器时，`/p/<profile>/...` 必须使用目标 profile 自己的 API key。[API：Multi-profile routing](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/api-server.md#multi-profile-routing-pprofile)
- 官方“多用户”说明是每位用户创建一个 profile、配置不同端口和 key，或使用复用路由；这不是完整组织/成员权限模型。[API：Multi-User Setup with Profiles](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/api-server.md#multi-user-setup-with-profiles)
- Web Dashboard 能管理状态、聊天、配置、API Key、会话、日志、分析、Cron、Profiles、Skills、MCP、Webhooks 与渠道；它是机器级管理面，可以切换并管理该机器上的任意 profile。[Web Dashboard](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/web-dashboard.md)、[Profiles：From the dashboard](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/profiles.md#from-the-dashboard)

**判断**

- Hermes 的 Runs API 比单纯 Chat API 更适合长任务：创建运行、订阅事件、查询状态、取消和处理审批是企业平台需要的通用原语。
- 机器级 Dashboard 不能直接当 SaaS 租户后台。目标项目需要按租户过滤所有列表与操作，并区分平台管理员、租户管理员、开发者、观察者和运行调用方。

### 6. 部署方式

**事实**

- Hermes 支持本机安装、长期 Gateway、Docker 和多种远程/云沙箱。官方 Docker 镜像支持多个 profile，并由 s6 管理每个 profile 的网关服务。[Docker guide](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/docker.md)
- 官方文档讨论了 Docker 网络出口隔离，并明确指出网络分段不能替代终端沙箱。[Network egress isolation](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/docs/security/network-egress-isolation.md)

**判断**

- Hermes 的部署选择很多，但这正是首版应削减的复杂度。推荐先支持 Docker Compose 单机企业部署，生产数据库用 PostgreSQL，沙箱只支持 Docker；云沙箱与 Kubernetes 后置。

## 三、对比

| 维度 | FastClaw | Hermes Agent | 对目标项目的含义 |
|---|---|---|---|
| 主要定位 | 多用户 Agent 工厂与运行平台 | 功能丰富、自改进的 Agent Runtime | 以前者学控制面，以后者学 Agent 内核 |
| 主要语言 | Go + Web 前端 | Python + Web/TUI/多入口 | 语言不是核心，边界和数据模型更重要 |
| Agent 表达 | 数据库中的 Agent + 系统文件 + 技能 | `HERMES_HOME` profile + `SOUL.md` + 记忆 + 技能 | 采用结构化 Agent 记录，保留文本人格/技能 |
| 最终用户隔离 | 用户/Agent 所有权；聊天者的 USER/MEMORY/SESSION 分区 | profile 状态目录；会话键和 profile key 分区 | 正式加入 tenant，并让最终用户数据单独分区 |
| 企业组织模型 | 未见完整组织/成员/RBAC 模型 | 未见企业组织模型 | 需要自行设计，不能照搬二者 |
| API | OpenAI 兼容 + 上游 users/usage/quota + 管理 API | Chat/Responses/Runs/Jobs/Sessions 等丰富 API | 运行 API 采用 Chat + Runs；管理 API 独立 |
| Web 管理 | 平台/用户/Agent 管理后台 | 机器级 profile 管理后台 | UI 信息架构可参考，权限模型必须重做 |
| 沙箱 | Docker/E2B/Boxlite，托管模式强制 | local 与多种容器/远程后端；profile 非沙箱 | 第一版只保留强制 Docker 沙箱，禁止宿主回退 |
| 单机状态 | SQLite + 本地目录 | SQLite/文件/profile 目录 | 开发模式可单机化 |
| 多副本 | PostgreSQL + 对象存储 + Redis | 主要围绕单机/profile gateway | 生产先 PostgreSQL，Redis/S3 随规模引入 |
| 技能 | 多层技能、ClawHub/skills.sh、按需加载 | agentskills.io、Skills Hub、自创建/自改进 | 首版只做版本化、批准后安装、按需加载 |
| 许可证 | 带额外限制的 source-available | MIT | 可更多复用 Hermes；FastClaw 以思想参考为主 |

## 四、最适合复用的思想

### 来自 FastClaw

1. **共享 Agent 模板与私有聊天者数据分离**：`SOUL/IDENTITY/技能` 属于 Agent；`USER/MEMORY/SESSION/FILES` 属于该 Agent 下的最终用户。
2. **上游最终用户映射**：上游应用使用稳定 `external_user_id`，平台内部生成 ID；映射必须绑定租户或 API Key，避免不同客户 ID 冲突。
3. **三种 API Key 范围**：平台管理、租户/用户、单 Agent。目标项目还应补充组织级服务账号和细粒度 scope。
4. **运行 API 与管理 API 分离**：OpenAI 风格的运行接口保持小而稳定；控制面接口管理 Agent 生命周期。
5. **环境变量只负责启动，数据库负责运行配置**：端口、数据库连接等走环境变量；模型、Agent、技能与策略走数据库和控制台。
6. **本地与生产共用内核**：本地 SQLite/单进程，生产 PostgreSQL/对象存储；执行计算与持久状态解耦。
7. **Agent 模板克隆**：复制人格、默认模型、技能绑定和工具策略，不复制记忆、会话和凭证。

### 来自 Hermes Agent

1. **同一 Agent 内核服务所有入口**：API、Web Chat、任务调度和将来的 IM 渠道都走同一运行流程。
2. **稳定的提示词分层**：人格、工具规则、技能摘要、用户记忆、上下文和临时请求有固定顺序，便于缓存、测试和审计。
3. **渐进加载技能**：提示词先放技能摘要，命中后再加载 `SKILL.md` 和参考文件。
4. **短期会话、长期事实、历史搜索分开**：不要把所有历史都塞进模型上下文。
5. **Runs 原语**：运行 ID、事件流、状态、取消、人工审批，是长任务和 Web 控制台的基础。
6. **工具注册与可用性检查**：模型只获得本次运行真正可用的工具 schema；服务端仍执行权限和参数策略。
7. **可中断、可观察**：工具开始/结束、输出、审批、错误和 token 用量都作为结构化事件记录。

## 五、第一版明确不应照搬的复杂功能

以下不是永远不做，而是不应成为轻量首版的前置条件：

- Telegram、Discord、Slack、WhatsApp、Signal 等大量渠道；首版优先 Web/API，最多选择一个企业 IM。
- 完整 Cron/提醒/Heartbeat/目标追踪/Kanban 编排。
- 多 Agent 自动委派、匿名子 Agent、跨 profile 编排与自动任务拆分。
- 自动学习技能、Agent 自动修改技能、多个技能市场、任意 URL 安装。
- 八种外部记忆提供方、知识图谱、复杂 RAG 与自动用户画像模型。
- 浏览器控制、电脑控制、语音、图像/视频生成、智能家居等大量内置工具。
- 七种终端后端和多种云沙箱；首版一个强制 Docker 沙箱足够。
- Coding Agent 项目预览、长驻开发容器、模板部署流水线。
- ACP、桌面 App、TUI、训练轨迹生成、多语言文档站。
- Redis 多副本协调、完整 S3 同步、HPA/PDB 等云原生能力一次性全部上线。
- FastClaw 的四层配置覆盖；首版先做“租户默认 + Agent 覆盖”，用户级个性化只保留必要字段。
- 允许最终用户在运行中安装任意技能或接入任意 MCP。
- 把宿主机 local 执行作为沙箱失败后的回退方案。

## 六、基于事实得出的产品边界建议

这部分是判断，不是两个上游仓库已经实现的事实。

### 建议的最小领域对象

- `Tenant`：企业/组织，安全和计费的第一边界。
- `User` 与 `Membership`：用户及其在租户中的角色。
- `AgentTemplate`：可发布、可版本化的人格、模型默认值、技能与工具策略。
- `Agent`：租户拥有的可运行实例，绑定模板版本和运行策略。
- `EndUser`：通过企业产品实际与 Agent 对话的人，可与控制台开发者分开。
- `Session`：对话容器，属于 Tenant + Agent + EndUser。
- `Run`：一次执行，记录状态、输入、输出、事件、token、成本和错误。
- `MemoryItem`：带来源、作用域、状态和删除能力的长期事实。
- `SkillVersion` 与 `ToolBinding`：不可变版本及 Agent 授权关系。
- `Sandbox`：短生命周期执行环境及配额、网络、挂载策略。
- `APIKey/ServiceAccount`、`Secret`、`AuditEvent`：企业接入与治理基础。

### 建议的首版能力

1. 租户、用户、成员角色和 API Key。
2. Agent 创建、克隆、草稿、发布、停用和版本回滚。
3. `SOUL.md` 风格人格 + 少量稳定系统规则。
4. OpenAI 兼容 Chat API，以及创建/查询/取消 Run 的 API 与 SSE 事件流。
5. 会话历史、少量长期记忆、删除和关闭自动记忆。
6. 版本化技能，人工审核后安装，按需加载。
7. 少量内置工具 + MCP 客户端，按租户/Agent 授权。
8. 强制 Docker 沙箱，含资源限制、工作区持久化、出网控制和审批。
9. Web 控制台：Agent、版本、运行、会话、记忆、技能、工具、密钥、用量和审计。
10. Docker Compose 开箱即用；生产默认 PostgreSQL，开发可选 SQLite。

### 必须从第一天做对的安全边界

- 每一条持久数据都能追溯到 `tenant_id`；数据库查询不依赖调用方自己记得加过滤条件。
- 服务端从认证身份解析 tenant，不能信任请求体里任意传入的 tenant。
- 密钥只在运行时按最小范围注入沙箱，不能进入提示词、日志、事件或长期记忆。
- 工具 schema 暴露前做授权，工具真正执行前再做一次策略检查。
- Agent 人格与技能不是安全策略；安全规则必须由服务端和沙箱强制。
- Agent、技能、工具配置必须有版本和发布状态，生产运行绑定不可变版本。
- 管理操作、审批、密钥变更、工具调用、沙箱创建与文件下载都进入审计日志。

## 七、尚未被上游事实证明的能力

做需求分析时不应因为仓库里存在相近名词就默认已经具备以下企业能力：

- 完整的组织/部门/项目/成员 RBAC；
- SSO/SAML/SCIM 和企业目录同步；
- 数据保留、导出、删除、区域驻留和合规策略；
- 对恶意 Agent、恶意技能、供应链攻击和跨租户侧信道的完整防护；
- 宕机中 Run 的自动迁移与精确一次执行；
- 成本结算、账单、发票和商业订阅全流程；
- 大规模多副本下的性能、稳定性和隔离压测结果。

这些应作为目标项目自己的需求和验收项，而不是从 FastClaw 或 Hermes 的功能清单中推定。


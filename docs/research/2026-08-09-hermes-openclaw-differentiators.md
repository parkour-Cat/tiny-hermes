# Hermes Agent 与 OpenClaw 的真实差异

> 调研日期：2026-08-09  
> Hermes Agent 核对版本：[`3f83297`](https://github.com/NousResearch/hermes-agent/tree/3f832978d30e0e14437edbf7a3f63315f08bad36)  
> OpenClaw 核对版本：[`d5809b4`](https://github.com/openclaw/openclaw/tree/d5809b4080fef042973029811dae98c2b99b3d16)  
> 资料范围：两个项目的官方仓库、仓库内官方文档和源码。未使用第三方评测或二手文章。

## 结论先行

Hermes 的差异不能概括为“有子 Agent、记忆、技能、自学习、长任务或上下文压缩”。在本次核对版本中，OpenClaw 也已经具备这些能力，而且部分实现更完整。

可以被一手资料支持的主要差异是：

1. **Hermes 的自动 Goal 循环更明确。** 它会在一轮结束后用辅助模型判断 `done / continue / wait`，未完成时自动发起下一轮，并可运行质量检查命令、等待后台进程和限制最大轮数。OpenClaw 的 Goal 主要是持久保存会话目标，并在后续用户回合持续提醒 Agent；官方文档没有表明它会自动连续发起新回合。
2. **Hermes 的训练数据导出更明确。** 它直接生成 ShareGPT JSONL，区分成功与失败轨迹，统一推理和工具调用格式，并提供批量生成与 Hugging Face 加载示例。OpenClaw 的 trajectory 官方定位是调试和支持用的“飞行记录器”，没有找到其直接生成训练集的官方说明。
3. **Hermes 的普通 HTTP Runs API 更直接。** 创建 Run、查询状态、订阅 SSE、停止和提交审批都有明确 REST 路径。OpenClaw 也有运行 ID、事件、等待、取消、任务台账和审批，但主要通过 Gateway WebSocket RPC 暴露，不能说它缺少运行控制能力。
4. **OpenClaw 的内置记忆体系更完整。** 它在 `USER.md`、`MEMORY.md`、每日笔记和 `DREAMS.md` 之间分层，并提供混合搜索、后台整理、来源与污染防护。Hermes 内置核心更轻，主要是有容量上限的 `USER.md + MEMORY.md` 与会话全文搜索；高级能力更多依赖外部记忆插件。
5. **OpenClaw 的子任务交付、技能治理和沙箱加固更明确。** 它对结果重试与保留、可见子会话、技能提案—扫描—应用—回滚、容器网络与权限限制给出了更完整的操作规则。Hermes 在市场来源和执行后端数量上更广，但“种类更多”不等于“更适合多租户托管”。

### 七项能力的判定总表

| 能力 | 主判定 | 准确含义 |
|---|---|---|
| 子 Agent / 多 Agent | **OpenClaw 更明确** | 两者都有隔离、并行和可嵌套子 Agent；OpenClaw 对交付重试、结果保留、可见会话、任务台账和工作树的说明更完整。 |
| 技能创建、修改与市场安装 | **共同能力，侧重点不同** | Hermes 的市场来源更广；OpenClaw 的提案、扫描、哈希绑定、审批、回滚和生命周期治理更适合生产控制。 |
| 记忆 | **OpenClaw 更明确或更强** | Hermes 内置记忆更轻；OpenClaw 的分层、检索、后台整理、来源和污染防护更完整。 |
| 长任务、Runs 与审批 | **Hermes 更明确，OpenClaw 并非缺失** | Hermes 在自动 Goal 和 REST Runs 上更直接；OpenClaw 的 Gateway RPC、任务台账和持久审批更完整。 |
| 上下文压缩与恢复 | **Hermes 算法更明确；整体强弱无法证实** | 两者都压缩、裁剪工具结果、保留最近消息和磁盘历史；没有统一故障测试证明谁更可靠。 |
| 沙箱与执行后端 | **OpenClaw 的隔离加固更明确；Hermes 后端更多** | Hermes 支持更多云端/HPC 后端；OpenClaw 对会话级容器、只读根目录、无网络和能力删除说明更清楚。 |
| 训练与自我改进闭环 | **Hermes 的训练导出更强；自我改进是共同能力** | Hermes 明确面向 ShareGPT/RL 数据；OpenClaw 的技能自学习治理反而更细。 |

## 1. 子 Agent / 多 Agent

**判定：共同能力；OpenClaw 在生产运维层更明确。**

### Hermes 已确认能力

- `delegate_task` 创建上下文隔离的子 Agent；每个子 Agent 有独立对话和终端会话，只有最终摘要进入父上下文。
- 支持多个任务并行运行、进度展示、取消和结果日志。
- 默认是一层父子结构；显式启用 `role="orchestrator"` 并提高 `max_spawn_depth` 后可以继续嵌套。
- 后台完成结果可持久保存，完成后重启仍能投递；但官方同时明确：**进程重启不会恢复仍在运行的子 Agent**，该次尝试会变成 `unknown`。

来源：[Hermes Delegation](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/delegation.md)

### OpenClaw 已确认能力

- `sessions_spawn` 默认创建隔离会话，也可用 `context: "fork"` 继承当前对话。
- 支持后台并行、父子结果回传、最多两层的受控嵌套和级联停止。
- 完成结果采用带幂等键的推送交付；直接交付失败后进入持久队列，自动重试最长 30 分钟，仍失败的成功结果保留 7 天供人工重试或取消。
- 控制台可查看父子会话树；还支持持久可见子会话、受管 Git 工作树和任务台账。

来源：[OpenClaw Sub-agents](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/subagents.md)、[OpenClaw Gateway Protocol：Tasks](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/gateway/protocol.md)

### 不能推出的结论

- 不能说“只有 Hermes 支持隔离或嵌套子 Agent”。
- 两边的“结果投递可恢复”都不等于“运行中的代码在宿主进程崩溃后能从断点继续”。是否能安全恢复还取决于工具副作用、幂等设计和任务存储，不能仅凭子 Agent 文档判断。

### 对 tiny-hermes 的建议

首版只做**一层父子委派**：子 Agent 是独立 Run，有明确任务、模型、工具、沙箱、费用上限和取消能力；父 Agent 只能读取结构化结果。先不做递归编排、持久线程绑定和受管工作树，但从数据模型中保留 `parent_run_id`、`depth` 和 `delivery_status`。

## 2. 技能创建、修改与市场安装

**判定：共同能力；Hermes 的市场来源更广，OpenClaw 的变更治理更明确。**

### 共同能力

- 两者都使用 `SKILL.md`，支持按需加载，而不是每轮把所有技能全文塞进提示词。
- 两者都能由 Agent 创建或修改技能，也都支持从公共来源安装技能。
- 两者都包含安全扫描、审批或人工复核、来源记录和生命周期管理，只是实现方式不同。

### Hermes 更明确的部分

- Skills Hub 支持官方技能、`skills.sh`、well-known URL、直接 URL、GitHub、自定义 tap，以及 ClawHub、LobeHub、browse.sh 等来源。
- 第三方安装记录来源 URL、内容哈希、扫描器版本、发现项、时间和信任级别；可检查上游变化、更新和重新审计。
- `skill_manage` 可直接创建、补丁、编辑、写支持文件或删除。打开 `skills.write_approval` 后，变更会先暂存并提供 diff；该审批默认关闭。
- 后台 review 与 Curator 可生成、修改、合并、归档技能，并提供快照与回滚。

来源：[Hermes Skills](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/skills.md)、[Hermes Curator](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/curator.md)、[Hermes Background Review 源码](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/agent/background_review.py)

### OpenClaw 更明确的部分

- Skill Workshop 把 Agent 生成内容先变成提案，再经过检查、应用、拒绝或隔离；更新提案绑定当前技能哈希，目标变化后提案会失效。
- 默认自动学习也不让后台 reviewer 直接写生产技能：小范围的新建和补丁可走扫描后自动应用，全文重写仍需人工复核。
- 应用前再次安全扫描，保存旧技能和支持文件用于回滚；随后进入 Curator 的过期、归档、固定和恢复流程。
- ClawHub、Git、`skills.sh` 和本地目录都可安装；ClawHub 提供信任包、安全扫描状态和来源验证，平台还可配置本地安装策略并失败关闭。

来源：[OpenClaw Skills](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/skills.md)、[OpenClaw Skill Workshop](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/skill-workshop.md)、[OpenClaw Self-learning](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/self-learning.md)

### 对 tiny-hermes 的建议

首版实现**版本化技能提案**，不允许 Agent 直接改已发布版本：

1. Agent 或开发者创建草稿；
2. 生成 diff，执行静态扫描；
3. 人工批准后发布不可变版本；
4. Agent 明确绑定版本并可回滚。

公共市场搜索、自动后台学习和 Curator 先只保留接口。首版可支持人工上传或 Git 导入，但不应允许最终用户在运行中安装任意远程技能。

## 3. 记忆

**判定：OpenClaw 的内置记忆更明确或更强。**

### Hermes 已确认能力

- 内置长期记忆只有 `MEMORY.md` 和 `USER.md` 两层，默认分别限制为 2,200 和 1,375 个字符。
- 每个新会话把两份记忆作为冻结快照放入系统提示词；本轮写入要到下一会话才进入该快照。
- 支持增加、替换、删除、安全扫描和可选的写入审批。
- 所有会话进入 SQLite，并用 FTS5 搜索原始消息；此外可安装多种外部记忆提供方。

来源：[Hermes Memory](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/memory.md)

### OpenClaw 已确认能力

- 内置分为 `USER.md`、`MEMORY.md`、每日笔记和 `DREAMS.md`；详细记录留在每日笔记，精炼后的长期事实才进入常驻记忆。
- 内置 SQLite 记忆引擎支持关键词、向量和混合检索；跨对话召回有同 Agent、私聊等边界。
- Dreaming 在后台收集、评分、去重和晋升候选记忆；不可信或系统生成的候选受到 taint gate 限制，不进入长期晋升路径。
- 记忆架构明确记录来源、证据、过期/替换关系和“何时可以采取行动”；还将事件触发的未来事项单独建模为 standing intent。
- 上下文压缩前默认先执行一次静默记忆刷新，降低摘要遗漏关键事实的概率。

来源：[OpenClaw Memory](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/memory.md)、[OpenClaw Memory Architecture](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/memory-architecture.md)、[OpenClaw Active Memory](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/active-memory.md)

### 对 tiny-hermes 的建议

首版只实现三类数据：

- `UserProfileItem`：稳定偏好；
- `MemoryItem`：经确认的长期事实；
- `SessionMessage`：完整会话历史，按需搜索。

每条记忆必须带租户、Agent、最终用户、来源会话、创建时间、状态和删除记录。先做关键词搜索或数据库全文检索，不做 Dreaming、知识图谱、跨 Agent 共享和多个外部记忆插件。

## 4. 长任务、Runs 与审批

**判定：Hermes 在自动连续执行和 REST Runs 上更明确；OpenClaw 的运行控制与审批并不缺失。**

### Hermes 的明确差异

- `/goal` 会在每轮后调用辅助 judge，返回 `done / continue / wait`；未完成时自动发起下一轮。
- Completion contract 可描述结果、验证方式、约束、范围和停止条件；质量门使用真实 shell 命令，必须退出码为 0 才能完成。
- judge 能等待后台进程、触发模式或固定时间，到条件满足后自动继续；默认最多 20 个连续回合。
- REST API 提供 `POST /v1/runs`、状态查询、SSE 事件、停止和审批接口；Jobs API 管理后台/定时任务。

来源：[Hermes Goals](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/goals.md)、[Hermes API Server](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/api-server.md)

### OpenClaw 的对应能力

- `/goal` 保存一个会话目标，支持状态、暂停、继续、阻塞、完成和 token 预算；目标随会话持久化并在后续回合注入。
- 官方文档把 Goal 明确称为会话状态，而不是后台任务；没有描述 Hermes 那种每轮 judge 后自动反复发起下一轮的行为。
- `agent` RPC 立即返回 `runId`，`agent.wait` 等待结束；Gateway 事件流包含模型、工具和生命周期事件，支持超时、AbortSignal 和会话取消。
- Gateway 还提供任务台账、会话订阅、持久审批记录、审批查询与解决，以及审批事件；执行命令的审批会绑定规范化命令计划，批准后若调用方修改命令或工作目录会被拒绝。

来源：[OpenClaw Goal](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/goal.md)、[OpenClaw Agent Loop](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/agent-loop.md)、[OpenClaw External Apps](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/gateway/external-apps.md)、[OpenClaw Gateway Protocol](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/gateway/protocol.md)、[OpenClaw Exec Approvals](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/exec-approvals.md)

### 对 tiny-hermes 的建议

首版必须实现 Run，而不是只提供 Chat：

- `queued / running / waiting_approval / cancelling / completed / failed / cancelled` 状态；
- SSE 事件、状态查询、取消、超时和审批；
- 工具与子 Agent 都产生结构化事件；
- 每次外部副作用带幂等键和审计记录。

自动 Goal 连续循环先保留 `Goal`、完成条件、预算和质量门接口，不在首版自动反复运行。这样先把运行持久化、取消和审批做可靠，再增加 judge 循环，避免失控消耗和重复副作用。

## 5. 上下文压缩与恢复

**判定：两者都有成熟实现；Hermes 的默认算法写得更具体，但无法据此证明整体更可靠。**

### Hermes 已确认能力

- 两层触发：Gateway 在约 85% 上下文时做安全兜底，Agent 内部默认在 50% 触发主压缩。
- 先清理受保护尾部以外的旧工具结果，再保护开头、最近消息和至少一个真实用户消息；工具调用与结果不被拆开。
- 中间部分按 Goal、约束、进展、决定、文件、下一步等固定结构总结；后续压缩会更新上一份摘要。
- 默认在同一个 session ID 上原地压缩，旧消息软归档、仍可搜索和恢复；还支持模型阈值、Codex app-server 原生压缩和 Responses 服务端压缩。
- 官方文档也明确记录一个风险：若摘要模型上下文过小而总结失败，当前实现可能丢弃中间消息而不生成摘要。这说明“算法详细”不等于“不会丢信息”。

来源：[Hermes Context Compression and Caching](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/context-compression-and-caching.md)

### OpenClaw 已确认能力

- 默认接近上下文上限或收到 provider 的溢出错误时自动压缩并重试；手动 `/compact` 可指定摘要重点。
- 保留最近消息，保证工具调用与结果成对；旧工具输出还可通过 pruning 单独裁剪。
- 完整历史仍保存在磁盘，内置 SQLite 压缩器保持同一个会话身份。
- 默认 safeguard 模式提供更严格的摘要保护；支持标识符保留、压缩模型覆盖、压缩前记忆刷新和可插拔 provider，provider 失败后回退内置总结。

来源：[OpenClaw Compaction](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/compaction.md)、[OpenClaw Session Pruning](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/concepts/session-pruning.md)

### 无法证实的差异

- 没有同一组长对话、工具输出、崩溃注入和摘要质量测试，无法证明哪一方“压缩后记得更准”或“恢复更可靠”。
- 保存原始历史只能让系统以后搜索或人工恢复，不能让已经发生的外部工具副作用自动回滚，也不能保证进程崩溃后从任意指令位置继续。

### 对 tiny-hermes 的建议

首版采用一个可解释流程：裁剪旧工具结果 → 保护最近消息和工具调用组 → 生成结构化摘要 → 保存摘要与原始消息引用。摘要失败时必须继续保留原文或停止本轮，不能静默删除。先保存 `compaction_event`、摘要版本和覆盖的消息范围，原生 provider 压缩与可插拔引擎只保留接口。

## 6. 沙箱与执行后端

**判定：Hermes 后端数量更多；OpenClaw 的多租户隔离形态和默认容器加固更明确。**

### Hermes 已确认能力

- 终端后端包括 local、Docker、SSH、Singularity/Apptainer、Modal、Daytona 和 Vercel Sandbox。
- Docker 是进程内共享的长驻容器，默认跨 `/new`、`/reset` 和子 Agent 保留环境；Vercel 可用快照保存文件，但不恢复进程。
- local 直接使用宿主用户权限；因此 profile 或会话本身不是安全边界。

来源：[Hermes Tools & Toolsets](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/features/tools.md)、[Hermes Security](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/user-guide/security.md)

### OpenClaw 已确认能力

- 沙箱后端包括 Docker、Podman、SSH 和 OpenShell；可选择每 Agent、每会话或共享环境。
- Docker 默认无网络、只读根文件系统、删除全部 Linux capabilities，并启用 init 与 `no-new-privileges`；工作区可设为无访问、只读或读写。
- 远程 Node 可执行命令并在节点本地应用审批策略。
- 但沙箱总体默认仍是关闭的，`elevated` 也可显式逃逸到 Gateway 或 Node；用于托管平台时必须由服务端强制覆盖这些默认值。

来源：[OpenClaw Sandboxing](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/gateway/sandboxing.md)、[OpenClaw Exec](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/exec.md)、[OpenClaw Nodes](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/nodes/index.md)

### 对 tiny-hermes 的建议

首版只支持**每 Run 或每 Session 一个强制 Docker 沙箱**，禁止回退宿主机。最低要求包括非 root、只读根目录、能力删除、`no-new-privileges`、CPU/内存/磁盘/进程/时间限制、默认无外网、出网域名白名单和短期密钥注入。云沙箱、SSH、HPC 和共享长驻容器只保留执行器接口。

## 7. 训练与自我改进闭环

**判定：Hermes 的训练数据链路更明确；技能和记忆自我改进是共同能力，OpenClaw 的治理更细。**

### Hermes 的训练差异

- 轨迹保存为 ShareGPT 兼容 JSONL，明确用于训练数据、调试和强化学习数据集。
- 成功运行写入 `trajectory_samples.jsonl`，失败或中断运行写入 `failed_trajectories.jsonl`。
- 推理统一为 `<think>`，工具调用和工具结果转换为统一格式；批量 runner 记录工具成功/失败统计并可直接被 Hugging Face Datasets 加载。

来源：[Hermes Trajectory Format](https://github.com/NousResearch/hermes-agent/blob/3f832978d30e0e14437edbf7a3f63315f08bad36/website/docs/developer-guide/trajectory-format.md)

### OpenClaw 的轨迹定位

- trajectory 默认开启，记录提示词、模型消息、工具、运行事件、模型/插件/技能配置、费用与缓存信息。
- 导出物是经过脱敏、有大小限制的支持包，官方明确把它称为按会话的 flight recorder，用于调试和支持。
- 在核对的官方资料中，没有找到 ShareGPT、监督微调或强化学习训练集导出的说明。

来源：[OpenClaw Trajectory Bundles](https://github.com/openclaw/openclaw/blob/d5809b4080fef042973029811dae98c2b99b3d16/docs/tools/trajectory.md)

### 自我改进不是 Hermes 独有

- Hermes 的后台 review 会从对话中提取长期记忆或技能，写入可由审批门控制；Curator 再做生命周期整理。
- OpenClaw 同样在成功工作后运行隔离 review，将纠正和流程变成技能提案，并经过扫描、哈希绑定、应用、回滚和 Curator。
- 因此，Hermes README 中类似“唯一具有内置学习循环”的宣传不能作为当前版本的可证实差异。

### 对 tiny-hermes 的建议

首版从第一天保存结构化运行事件，但不建设训练平台。事件至少包含模型请求版本、消息角色、工具调用与结果、审批、错误、token、费用、父子 Run 和最终状态，并对密钥与个人信息脱敏。后续可以把这些事件转换为训练格式；ShareGPT/RL 导出、自动评估和数据集管理先只保留接口。

## 无法由官方资料证实的宣传或推断

以下说法不应进入 tiny-hermes 的产品依据：

- “只有 Hermes 有子 Agent、技能自学习、跨会话记忆或上下文压缩。”
- “OpenClaw 没有 Run、取消、事件或审批。”它主要通过 Gateway RPC 暴露，而不是 Hermes 风格的 REST Runs。
- “后端数量更多，所以沙箱更安全。”Hermes 后端更广，OpenClaw 的容器加固更细；安全强度仍需威胁模型和攻防测试。
- “保留了会话，所以崩溃后能精确继续。”会话恢复、结果投递恢复和执行恢复是三件不同的事。
- “默认开启轨迹就等于训练闭环。”OpenClaw 的默认轨迹是调试记录；Hermes 的训练导出也仍需要数据质量、许可、隐私和评估流程。
- “任一项目已经是企业多租户平台。”两者都主要是 Agent Runtime；组织、成员、RBAC、租户级密钥、配额、审计和数据删除仍需要 tiny-hermes 自行建设。

## tiny-hermes 首版最终取舍

### 首版应真正实现

1. 精简的单 Agent 工具循环，所有入口共用同一内核。
2. 持久化 Session 与结构化 Run；SSE、查询、取消、超时、审批和审计可实际工作。
3. 一层隔离子 Agent，每个子 Agent 都是独立、可观察、可取消的 Run。
4. 简单可靠的压缩：旧工具结果裁剪、结构化摘要、最近消息保护、原始历史保留和失败关闭。
5. `UserProfileItem + MemoryItem + SessionMessage` 三层记忆，具备来源、作用域、查看、修改和删除。
6. 技能草稿、扫描、diff、人工批准、不可变版本、绑定和回滚；支持人工上传或 Git 导入。
7. 每 Run/Session 强制 Docker 沙箱，不允许宿主机回退。
8. 从第一天保存脱敏的结构化运行事件，为未来训练导出留数据基础。

### 首版只保留接口或数据字段

- Hermes 式自动 Goal judge 连续循环、自动等待和质量门执行器；
- 两层以上嵌套 Agent、自由组队、跨 Agent 共享记忆和受管工作树；
- 对话结束后的自动技能学习、自动应用和 Curator；
- Dreaming、知识图谱、复杂向量记忆和多个外部记忆提供方；
- Cron、Heartbeat、standing intent、Kanban/Task Flow；
- ShareGPT/RL 训练导出、自动评分、数据集发布；
- Podman、SSH、HPC、云沙箱和远程 Node 等多执行后端。

这个取舍保留了 Hermes 的核心体验——人格、记忆、技能、工具、长任务和子 Agent——但把自动连续执行、自我修改和多后端扩张放在可靠的 Run、审批、版本和沙箱边界之后，更适合作为企业可部署的轻量首版。

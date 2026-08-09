# tiny-hermes M1 Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 分四个可独立运行、可自动验证的阶段交付 tiny-hermes 0.1 单 Agent 安全运行骨架。

**Architecture:** 保持一个仓库和一个 Python 后端发布单元，API、Worker、Scheduler、Sandbox Controller 与 Web 分进程运行。PostgreSQL 保存业务事实，Redis 只负责唤醒，MinIO 保存文件；每个阶段都从全新环境启动并打通一条真实链路。

**Tech Stack:** Python 3.12、uv、FastAPI、Pydantic、SQLAlchemy 2、Alembic、PostgreSQL、Redis、MinIO、React、TypeScript、Vite、Ant Design、Docker Compose、pytest、Vitest、Playwright

---

## 1. 路线图使用规则

本文只固定阶段边界、先后依赖与阶段出口，不替代逐文件实施计划。每进入一个阶段前，基于前一阶段已经跑通的代码再写该阶段的细化计划；不为尚未验证的接口一次性写完四个阶段的所有代码步骤。

权威输入：

- 产品范围与验收：`docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4。
- M1 进程、数据和接口设计：`docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1。
- 第一阶段细化计划：`docs/superpowers/plans/2026-08-10-tiny-hermes-m1-foundation.md`。

共同完成规则：

- 先写会失败的测试，再写刚好让测试通过的实现。
- 每个阶段必须支持从空数据库和空持久卷启动。
- 前端不得用假数据伪装后端能力。
- PostgreSQL 是状态真相；Redis 通知丢失不能造成业务数据丢失。
- 所有跨工作空间查询由服务端限定范围，不能依赖界面隐藏。
- 每阶段结束时运行该阶段全部检查，并保存简短验收记录。

## 2. 阶段依赖

```mermaid
flowchart LR
    F1["阶段一：可启动骨架"] --> F2["阶段二：Run 主链路"]
    F2 --> F3["阶段三：真实模型与沙箱"]
    F3 --> F4["阶段四：产品闭环"]
    F4 --> M1["0.1 Technical Preview 验收"]
```

不能并行倒置的原因：

- Run 必须引用真实 Workspace、调用主体和审计机制，因此阶段二依赖阶段一。
- 沙箱恢复依赖 Run 检查点、租约和事件，因此阶段三依赖阶段二。
- Playground、Chat Completions 与性能验收必须消费真实 Run、模型和沙箱，因此阶段四依赖前三阶段。

## 3. 阶段一：可启动骨架

**可运行成果：** 在全新本地环境启动 PostgreSQL、Redis、MinIO、API 和 Web；使用一次性 Bootstrap Token 创建首位平台管理员，登录后创建两个 Workspace，查看自己的成员身份；所有管理写操作都有 AuditEvent。

**包含：**

- 仓库目录、Python/Node 版本约束、依赖锁文件和基础命令。
- FastAPI 与 React/Vite 骨架。
- 配置加载、结构化日志、`/health/live`、`/health/ready`。
- Compose 基础依赖与一次性 Alembic migration。
- `users`、`auth_identities`、`auth_sessions`、`workspaces`、`memberships`、`audit_events`。
- Bootstrap、登录、当前用户、退出、Workspace 创建与列表。
- 最小初始化/登录/Workspace 页面。
- 后端、前端和迁移的 CI 基础检查。

**明确不包含：** Agent、Session、Run、Worker 领取、Scheduler 扫描、模型调用、沙箱、Secret、ServiceAccount、API Key。

**出口检查：**

- Bootstrap 入口在首位管理员创建后永久关闭。
- 密码只保存摘要，浏览器会话只保存随机令牌摘要并可撤销。
- 无 `X-Workspace-Id` 的工作空间资源请求被明确拒绝；无成员身份不能访问已知 Workspace ID。
- 初始化、登录、退出、创建 Workspace 与成员变化均写脱敏 AuditEvent。
- API 连接不上数据库或 migration 落后时 `/health/ready` 返回非成功状态。
- `uv run pytest`、静态检查、前端单测与构建全部通过。

## 4. 阶段二：Run 主链路

**可运行成果：** 发布一个使用确定性模型替身的 AgentVersion，通过 Runs API 创建任务并从 SSE 看到连续事件；同 Session 的消息严格按 FIFO 执行，可暂停、继续、取消并从安全的 failed Run 派生重试。

**包含：**

- Agent、AgentDraft、不可变 AgentVersion 与发布/回滚。
- Session、CanonicalMessage、Run、RunBudgetScope、RunEvent 与 IdempotencyRecord。
- 权威状态机、`state_version`、Session head/pending 队列和队首修复。
- Run 创建幂等、RunEvent 连续序号、控制操作与安全重试。
- WorkerLease、Worker 数据库轮询、Redis 唤醒和 Scheduler 基础扫描。
- SSE 续读、410 过期游标行为和确定性模型替身。
- 最小 Agent 草稿、发布、Run 列表与事件查看页面。

**逐文件计划触发条件：** 阶段一出口检查全部通过，数据库迁移和身份端口已经稳定。

**出口检查：**

- 并发相同 Idempotency-Key 只产生一个 Run。
- API、Worker、Scheduler 并发写事件仍得到唯一连续序号。
- Run1 执行、Run2 已取消、Run3 排队时，Run1 完成后 Run3 成为 head。
- Runs API 在阻塞 Session 中成功创建 pending Run并返回队列说明。
- failed Run 重试共享根预算，并发派生默认最多成功 3 个。
- Worker 或 Redis 重启不会丢失已提交状态。

## 5. 阶段三：真实模型与沙箱

**可运行成果：** Agent 通过 OpenAI 兼容模型端点，在强制 Docker 沙箱内连续执行文件与受限命令工具；已提交的 `/workspace/data` 在故障后恢复，沙箱无法联网且失败时不回退宿主机。

**包含：**

- Provider 中立消息格式与 OpenAI 兼容适配器。
- SafeOutboundClient、地址拒绝、重定向复查与禁止旁路检查。
- usage 质量、模型/工具次数和双时间限制累计。
- Sandbox Controller 内部协议、Reservation/Lease 所有权检查和 Docker 最低策略。
- `file.read`、`file.write`、`file.list`、`command.run` 两次授权。
- `/workspace/data` revision CAS、ObjectUpload staging、Artifact。
- 同 Run 沙箱保温、跨 Run 新可写层、`cache_state=reset` 提示。
- Scheduler 对租约、沙箱和 staging 对象的回收。

**逐文件计划触发条件：** 阶段二状态机、检查点、租约与事件模型通过并发和故障测试。

**出口检查：**

- 未绑定工具即使模型提出合法调用也被执行层拒绝。
- 路径逃逸、符号链接逃逸和宿主机执行回退均被自动测试拒绝。
- 同一时间片多工具步骤不重建容器；idle TTL 内重新获取容器 ID 不变。
- WorkspaceRevision 冲突进入 `interrupted`，不覆盖新 revision。
- loopback、元数据地址、未批准私网和危险重定向全部被拒绝。

## 6. 阶段四：产品闭环

**可运行成果：** 开发者可以在最小控制台创建并发布 Agent，在 Playground 运行任务、查看时间线和文件；API 客户端可使用 API Key 调用 Runs API 或兼容 Chat Completions，随后完成 0.1 的安全、故障、性能和部署验收。

**包含：**

- 完整 M1 Agent Builder、Playground、Run Detail 与国际化。
- ServiceAccount、API Key scope、撤销和一次性明文返回。
- 默认一次性 Session 与显式持久 Session 的 Chat Completions。
- 两类 `session_blocked` 行为和 `requires_runs_api`。
- Secret 信封加密与 KEK 重包基础演练。
- 跨 Workspace、安全、故障、恢复和性能基准。
- Linux 参考环境部署文档、升级检查和 0.1 验收记录。
- 飞书 WebSocket 长连接技术验证事实记录，不交付飞书适配器。

**逐文件计划触发条件：** 阶段三已用真实模型端点和 Docker 沙箱完成端到端文件任务。

**出口检查：**

- 产品设计 §27.1 的 13 个 M1 场景全部有自动检查或有证据的人工验收记录。
- 产品设计 §24.1 的全部门槛在指定 Linux 参考环境通过并保存原始结果。
- Runs API、SSE、控制台和 Chat Completions 对同一 Run 状态表达一致。
- 数据库迁移、Secret 重包与恢复演练通过。
- 飞书记录明确区分已实测事实、尚未确认项和 Webhook 兜底决定。

## 7. 0.1 发布关口

只有以下条件同时满足才能标记 `0.1 Technical Preview`：

- 四阶段出口检查全部通过。
- 全新 Linux Docker 主机可按文档启动并完成单 Agent 文件与命令任务。
- 发布物没有真实 Secret、本地数据、测试产物或未声明依赖。
- 项目描述使用“单 Agent 安全运行骨架”，不宣传为已经完成企业级多 Agent 平台。
- 创建 M2 细化计划前先回顾 0.1 的安装、失败 Run、沙箱启动、API 使用与开发者反馈。

# 指标与链路追踪

日期 2026-10-22。状态：**已实现**（分支 `feat/observability`）。实现与初稿不同的地方见 §9。
依据：产品设计 §21.5（「OpenTelemetry trace、Prometheus 指标」），
[容量方案](../plans/2026-09-29-enterprise-capacity.md)，
[单机档验收](../verification/2026-09-30-tier-a-acceptance.md)。

## 1. 要解决什么

§21.5 从第一版起就写着 Prometheus 指标和 OpenTelemetry trace，代码里一个都没有：
依赖里没有 `prometheus_client`，也没有 `opentelemetry`。现在能看到的只有三样东西：

- 结构化日志（`shared/logging.py`）；
- `api` 的 `/health/live`、`/health/ready`，以及其余进程的「进程还活着」；
- `run_events` 表里的业务事件。

容量方面的数字都是 `scripts/concurrency_probe.py` 在压测时从 Run 行的时间戳和
`docker stats` 采样得来的。**压测之外，没有任何办法知道线上此刻的情况**：

- 有多少 Run 在排队，最老的一个等了多久；
- 每个 Worker 的通道用满了没有；
- 模型调用变慢了没有；
- 沙箱准入是不是在拒绝；
- scheduler 是不是卡在某一项扫描上。

这些问题现在只能等用户抱怨了才知道。

这份设计回答两件事：**要量什么**，**怎么量才不破坏现有的安全边界**。
第二件比第一件难，见 §4。

## 2. 不做什么

- **不拿指标或 trace 代替审计。**§21.5 已经写明：审计是业务数据，有权限和保留期。
  指标和 trace 是运维数据，可以采样、可以丢。
- **不在指标里放工作空间、用户、Run 的 id。**每多一个这样的标签，就多一组时间序列，
  序列数会随租户数和 Run 数无上限地涨。要查某一个 Run，看它的 trace 和日志。
- **不把提示词、工具参数、工具输出、模型回复放进 trace 或指标。**这些可能含有业务数据和
  密钥。span 上只放 id、种类、状态、耗时。
- **不托管 Prometheus、Grafana、追踪后端。**Compose 里给一个可选的 profile 方便本地和
  小规模部署；企业可以接自己的监控。
- 这一遍**不做告警通知渠道**（邮件、飞书）。只给告警规则，通知由企业自己的 Alertmanager 配。

## 3. 量什么

按「出了问题，运维第一个想问什么」排，不按进程排。命名用 `tiny_hermes_` 前缀，
时长单位都是秒。

### 3.1 排队：容量够不够

这一组最重要。容量方案的结论是「按同一时刻正在执行的 Run 数规划」，这组指标就是那个数。

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `runs_by_status` | gauge | `status` | 各状态的 Run 数，从数据库读 |
| `queue_oldest_age_seconds` | gauge | — | 可领取的 head Run 里等得最久的那个已经等了多久 |
| `claim_wait_seconds` | histogram | — | 从可领取到被领取的时长，Worker 领到时记 |
| `worker_lanes` | gauge | `state`（`total`/`busy`） | 每个 Worker 进程 K 条通道：`total` 是在跑的通道数，`busy` 是正在执行时间片的 |

`runs_by_status` 和 `queue_oldest_age_seconds` 要查数据库，见 §5.2：谁来查，以及为什么
不放进 scheduler 的串行循环。

### 3.2 执行：慢在哪一段

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `model_call_seconds` | histogram | `provider`（deterministic/openai_compatible）、`outcome`（停止原因或 error） | 一次模型调用 |
| `model_tokens_total` | counter | `provider`、`direction`（in/out） | 只计提供方报告了的用量 |
| `tool_call_seconds` | histogram | `kind`（file/shell/http/mcp/platform）、`outcome` | 按种类，不按工具名 |
| `sandbox_acquire_seconds` | histogram | `outcome`（cold/warm/refused/unreachable/failed） | 含准入排队 |
| `workspace_commit_seconds` | histogram | `outcome` | 每轮提交工作区；[每轮开销记录](../verification/2026-09-29-round-overhead.md)说它是单机的主要开销 |
| `runs_finished_total` | counter | `status` | 事务提交时才计数，回滚的不算 |
| `recoveries_total` | counter | `outcome`（approved/failed） | 中断恢复，同样提交时才计数 |

按提供方类型而不是端点分：端点是工作空间自己建的，数量没有上限。工具名是 Agent 开发者随便起的，
数量也没有上限，所以工具只按种类分。

### 3.3 后台：scheduler 有没有在干活

scheduler 的 `run_once` 是串行的 14 项扫描（`runs/application/scheduler.py:115`）。

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `scheduler_scan_seconds` | histogram | `scan` | 每一项扫描各自的耗时 |
| `scheduler_scan_errors_total` | counter | `scan` | |
| `scheduler_scan_last_success_timestamp` | gauge | `scan` | 每一项最近一次成功跑完的时间 |

**读代码时发现的问题已经单独修了**（PR #124，
[验收记录](../verification/2026-10-22-scheduler-scan-isolation.md)）：原来任何一项扫描抛异常，
这一轮剩下的全部跳过；现在每项单独捕获，其余照跑。所以「整轮跑完」不再说明什么，要看的是
**每一项**最近一次成功是什么时候：一项每次都失败，别的照常，只有它自己的时间戳停住。

### 3.4 边界：沙箱和出口

| 进程 | 指标 | 类型 | 标签 |
|---|---|---|---|
| controller | `sandboxes` | gauge | `state`（running/frozen） |
| controller | `sandbox_memory_reserved_bytes`、`sandbox_memory_budget_bytes` | gauge | — |
| controller | `sandbox_admission_rejected_total` | counter | — |
| controller | `controller_action_seconds` | histogram | `action`、`outcome`（ok/refused） |
| egress-proxy | `egress_connections_total` | counter | `caller`（调用方类型）、`decision`（allow/deny）、`reason` |

出口不按目标主机打标签：主机由 Agent 决定，数量没有上限。要查某个被拒的连接，看日志。

### 3.5 入口

| 指标 | 类型 | 标签 |
|---|---|---|
| `http_request_seconds` | histogram | `route`（路由模板，不是实际路径）、`method`、`status_class`（2xx/4xx/5xx） |
| `sse_connections` | gauge | — |
| `login_attempts_total` | counter | `outcome`（ok/bad_credentials/locked） |

`login_attempts_total` 也是之后做登录限流时的依据。

### 3.6 不自己写的

PostgreSQL 和 Redis 用社区的 `postgres_exporter`、`redis_exporter`，放在同一个 Compose
profile 里。单机档验收里 Postgres 是最先撞墙的，它的连接数、锁等待、慢查询必须看得到，
但这些不该由我们重新实现。

## 4. 怎么暴露才不破坏边界

这一节是这份设计的重点。几个进程是安全边界，给它们开一个监听端口，就是在边界上开一个口子。

### 4.1 指标端口不对外

- `api` 的 8000 端口**不提供** `/metrics`。它经网关对外，指标会泄露路由、登录失败次数
  这类信息。
- 每个进程另开一个只给 Prometheus 用的端口（默认 9464），**不映射到宿主机**
  （Compose 里不写 `ports:`），只在 Compose 的 `default` 网络上可达。
- Worker、scheduler、controller、egress-proxy 现在都没有 HTTP 服务。用 `prometheus_client`
  自带的 HTTP 服务开一个只读端点，不复用业务端口。

### 4.2 egress-proxy 的指标端口不能让沙箱访问到

egress-proxy 同时接在 `default` 和 `sandbox-egress` 两个网络上（`compose.yaml` 的
`egress-proxy.networks`），沙箱能连到它。它的指标端口如果监听 `0.0.0.0`，**任何沙箱都能读到
全平台的出口放行和拒绝计数**，包括别的工作空间的。

做法（实现时改过，见 §9）：指标端口可以要求 `Authorization: Bearer <METRICS_TOKEN>`；
**egress-proxy 没有 token 就不提供指标**。初稿想的是只在 `default` 网络那一侧的地址上监听，
但容器里拿不到「哪个地址属于哪个 Compose 网络」的可靠信息：按主机名解析、按网卡枚举都要
猜。token 不用猜，沙箱拿不到它。
验收时要**在真实沙箱里**连这个端口，确认拿不到指标。光读配置不算数。

controller 不接 `sandbox-egress`，没有这个问题。但 B 档里 controller 在沙箱主机的内网
地址上对外（运维手册 §7），指标端口同样只开在 Prometheus 能到的那个地址上，并写进 §7 的
防火墙说明。

### 4.3 trace 上下文不发给外部

OpenTelemetry 的 httpx 自动插桩会给每个出站请求加 `traceparent` 头。我们的出站请求是发给
模型提供方和企业的业务 API 的。**把内部的 trace id 发给第三方，是信息泄露。**

所以：

- 不对 `tiny_hermes.outbound` 用自动插桩。在那里手动开 span，**只记录，不注入请求头**。
- 内部调用（Worker → controller）才传 trace 上下文，见 §5.3。
- 写一条测试：经替身 API 发一个 HTTP 工具调用，断言收到的请求头里没有 `traceparent`
  和 `tracestate`。

## 5. 怎么实现

### 5.1 依赖与开关

- 指标：`prometheus_client`。**默认开**，因为它只是一个不对外的端口。
- trace：`opentelemetry-sdk` 和 `opentelemetry-exporter-otlp`。**默认关**：
  `OTEL_EXPORTER_OTLP_ENDPOINT` 不设就用 no-op，不连任何地方。
- 采样：在建 Run 时决定，按 `OTEL_TRACES_SAMPLER_ARG` 的比例（Compose 默认 1.0，生产建议 0.1，见 §8），同一个 Run
  的所有时间片跟随这个决定，不会出现只采到半个 Run 的情况。

### 5.2 数据库派生的 gauge 谁来查

`runs_by_status` 和 `queue_oldest_age_seconds` 要查库。三种放法：

| 放在 | 问题 |
|---|---|
| 每个 Worker | M 个进程重复查，数字也重复 |
| `api`，在被抓取时查 | 每次抓取都要查一次库；N 个 api 进程查 N 次 |
| **scheduler，独立的 asyncio 任务，每 15 秒查一次** | 只有一份。缺点是 scheduler 挂了数字就停，靠 `scheduler_scan_last_success_timestamp` 发现 |

选第三种。但**不放进 `run_once` 的串行循环**，而是单独一个任务：否则某一项扫描一慢，
排队数字就跟着停。而扫描变慢的时候，恰恰最需要看排队。查询用现有的部分索引
（[队首巡检记录](../verification/2026-09-30-head-scan.md)），实现时用 `EXPLAIN` 确认不是全表扫描。

### 5.3 一个 Run 的 trace

Run 是异步的：api 建了 Run 就返回，Worker 可能几秒后、也可能几小时后才领到，中间可能换好几个
Worker、经历好几次中断恢复。trace 上下文要跨过数据库：

1. api 建 Run 时开一个 span，把 W3C `traceparent` 写进 Run 行的新列 `trace_context`。
2. Worker 领到 Run 后，以它为父开 `slice` span。下面挂子 span：模型调用、每个工具调用、
   沙箱获取、工作区提交。
3. Worker 调 controller 时，在控制行里加一个可选字段带上 `traceparent`；controller 以它为父
   开 span。旧的 controller 不认识这个字段时直接忽略，B 档两边可以分开升级。
4. scheduler 恢复一个 Run 时，同样以 `trace_context` 为父开 span，这样「中断 → 恢复」在同一条
   trace 里看得见。
5. 日志里加 `trace_id`、`span_id` 两个字段（`shared/logging.py` 已经会追加 `key=value`），
   日志和 trace 可以互相跳转。

一个跑几个小时的 Run 会是一条很长的 trace。可以接受：要看的往往就是「这一个 Run 时间花在哪」。

### 5.4 Compose 的 `observability` profile

`docker compose --profile observability up` 额外起：

- Prometheus：用 Docker DNS 发现 `worker` 等服务的全部副本
  （`--scale worker=M` 时，服务名会解析出 M 个地址）；
- Grafana：带一个预置的仪表盘，按 §3 的分组排面板；
- 一个 OTLP 兼容的追踪后端（Jaeger）；
- `postgres_exporter`、`redis_exporter`。

镜像都钉版本和摘要，和 `compose.yaml` 里其他镜像一样。

### 5.5 告警规则（只给规则，不配通知）

| 规则 | 条件（初值，试点后调） | 为什么 |
|---|---|---|
| 排队过久 | `queue_oldest_age_seconds` > 60，持续 5 分钟 | 容量不够的第一个信号 |
| 通道满 | 所有 Worker 的 busy ÷ 总数 > 0.9，持续 10 分钟 | 该加 Worker 了 |
| scheduler 某项停了 | 任一 `scan` 距 `scheduler_scan_last_success_timestamp` 超过 5 倍扫描间隔 | 一项反复失败，或整个 scheduler 停了 |
| 沙箱准入拒绝 | 每分钟拒绝数 > 0，持续 10 分钟 | 沙箱主机内存不够 |
| 出口拒绝突增 | 拒绝率是过去 1 小时的 5 倍 | Agent 配错，或有东西在试探 |
| 登录失败突增 | 每分钟失败 > 30 | 有人在猜密码 |

阈值都是初值，和容量方案 §6 的四条标准一样，要等试点之后写进规格。

## 6. 分几步

每一步按项目的规矩：先红测试，再实现，再有验收记录。

1. **指标的骨架**：`shared/metrics.py`（注册表、端口、按进程启动），五个进程各开端口；
   §3.1 和 §3.3。测试：真 Worker 用确定性模型跑完一个 Run，抓 `/metrics`，断言
   `claim_wait_seconds` 有样本、通道忙碌数在时间片里加一；`api` 的 8000 端口上 `/metrics` 是 404。
2. **其余指标**：§3.2、§3.4、§3.5。egress-proxy 的监听地址加真实沙箱测试（§4.2）。
3. **Compose profile 和仪表盘**（§5.4、§5.5），运维手册加一节。
4. **trace**：迁移加 `runs.trace_context`；§5.3 的 1–5；§4.3 的不注入测试。
5. **验收**：起 `observability` profile，按单机档验收的第 1、4 档重跑 `concurrency_probe`，
   核对三件事：
   - Prometheus 的目标列表里，Worker 恰好 M 个，都是 up；
   - Grafana 上的同时执行数，和探针从数据库读到的一致；
   - 随便挑一个 Run，在 Jaeger 里能看到 api → 多个时间片 → controller 的整条链。

   这一步证明的是「这条路走得通」，不是「测试过了」。

## 7. 和产品设计的关系

不冲突，是在补 §21.5 的空白。定稿后在产品设计里记一次修订（v2.18），写进三条决定：

- 指标端口不对外；
- trace 上下文不发给外部服务；
- 指标不带租户和 Run 的 id。

## 8. 已决定（2026-10-22）

1. **trace 采样率**：本地和试点 100%；企业正式部署默认 10%，用 `OTEL_TRACES_SAMPLER_ARG` 调。
   哪个 Run 被采在建 Run 时决定，没被采到的 Run 只能看日志和指标。
2. **scheduler 的「一项失败，整轮跳过」**（§3.3）：要修，单独一个 PR，先于指标。
3. **追踪后端**：Compose profile 用 Jaeger。记录一侧只用 OpenTelemetry 标准，换后端不改代码。

## 9. 实现与初稿的不同

| 初稿 | 实现 | 为什么 |
|---|---|---|
| egress-proxy 只在 `default` 网络一侧监听指标端口 | 指标端口要 bearer token，egress-proxy 没有 token 不提供指标 | 容器里分不清哪个地址属于哪个网络（§4.2） |
| `worker_lanes{state=busy/idle}` | `state=total/busy` | 空闲数 = 总数 − 忙碌数，存两个原始量更不容易出错 |
| `model_alias` 标签 | `provider` 标签 | 端点由工作空间创建，没有上限（§2） |
| `slice_seconds` | 没有做 | 时间片的时长由 `WORKER_MAX_SLICE_SECONDS` 决定，看不出慢在哪；慢在哪由模型、工具、沙箱、提交四项回答 |
| `runs_finished_total{status, reason}` | 只有 `status` | `failure_reason` 的取值没有封闭的集合 |
| `docker_op_seconds{op}` | `controller_action_seconds{action, outcome}` | 在 controller 的协议层计时，包含 Docker 和数据库，就是 Worker 实际等的时间 |
| `scheduler_last_cycle_timestamp` | `scheduler_scan_last_success_timestamp{scan}` | PR #124 之后一项失败不再跳过整轮，「整轮跑完」不再说明问题 |

另外两条：

- **OTLP 导出不经过 egress-proxy。**导出目标是部署自己网络里的服务（Compose 里是 Jaeger），
  和 PostgreSQL、Redis 一样。OpenTelemetry 的导出器自己开连接，不经过 `SafeOutboundClient`。
- **流式的 controller 动作（导入、导出工作区）没有 span。**只有普通的请求-应答动作加入了
  调用方的 trace。


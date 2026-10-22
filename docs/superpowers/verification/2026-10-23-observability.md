# 指标与链路追踪

日期 2026-10-23。分支 `feat/observability`。设计：[指标与链路追踪](../specs/2026-10-22-observability-design.md)
（实现与初稿的不同见其 §9），产品设计 v2.18（§1.18、§21.5），运维手册 §12。

## 为什么有这一遍

产品设计 §21.5 从第一版起就写着 Prometheus 指标和 OpenTelemetry trace，代码里一个都没有。
容量数字只能在压测时从 Run 行和 `docker stats` 采样得来，线上此刻排了多少队、慢在哪一段、
scheduler 有没有在干活，都看不到。

## 改了什么

| 步 | 内容 | 测试 | 实现 |
|---|---|---|---|
| 1 | 排队、通道、scheduler 每项扫描的指标；每个进程一个内部指标端口 | `06804a6`、`bbe8da3` | `4a9f0fc` |
| 2 | 模型、工具、沙箱、工作区提交、controller、出站代理、API 的指标；指标端口可要求 token | `3ed202c` | `2f2161c` |
| 3 | Compose `observability` profile：Prometheus、Grafana（预置仪表盘）、Jaeger、两个 exporter；告警规则 | `0d13bdf`、`1936e86` | `a49a17f` |
| 3 | 指标端口被占用时记日志、进程照常运行 | `f80de87` | `7ba2055` |
| 4 | 一个 Run 一条 trace：Run 行存 `traceparent`，时间片、模型、工具、沙箱、提交、恢复、controller 动作都在它下面；日志带 `trace_id` | `444f132` | `f9a6909`、`6f491b5` |

### 写的时候发现的两件事

- **一项扫描失败，整轮跳过**。读 scheduler 时发现，已经单独修了（PR #124）。所以 scheduler
  的指标是「每一项最近一次成功的时间」，不是「整轮最近一次跑完的时间」。
- **构建镜像拉不到 PyPI。**新依赖第一次进镜像时，`uv sync` 在构建容器里连不上 PyPI，就是
  CLAUDE.md「跑不动的时候」那一节说的情况，以前一直被构建缓存盖着。这一遍用
  `--build-arg HTTPS_PROXY=http://proxy.orb.internal:8305 --build-arg HTTP_PROXY=...` 绕过去
  （OrbStack 守护进程自己用的就是这个代理）。只是本机的情况，没有改任何构建文件。

## 测试过了

- 后端单元测试 2680 passed；集成测试（含 sandbox 目录）1237 passed、1 skipped（原有的 TLS
  socket 权限那一条）；pyright 0 errors；`ruff check` 通过；`alembic check` 无差异，新迁移
  降级再升级通过。
- 全量集成跑完之后又改了一处（trace 提取改成按函数名调用，见下面「发现」第 3 条），之后
  重跑了全部单元测试和 `test_tracing`、`test_transport`，没有重跑全量集成。
- compose-e2e 以 PR 的 CI 为准。
- **PR 的第一次 CI，backend-unit 红了 3 条**（`test_cli.py` 里启动 api 和 scheduler 的入口）：
  入口现在要先读配置才能起指标和 trace，而 CI 的单元测试没有 `.env`，也没有 `DATABASE_URL`。
  本地是绿的，只因为仓库根目录的 `.env` 补上了配置。在本地把 `.env` 挪开就复现了。修法是把
  指标和 trace 放进 `_observe` 一个钩子，测试按原来替换 `configure_logging` 的办法把它也替换掉
  （`9eb4717` 测试，之后一个提交实现）。挪开 `.env` 重跑全部单元测试：2680 passed。

新测试覆盖的：

- 领取等待、通道忙碌数（时间片里加一、结束后回去）、通道总数随 `run_forever` 增减。
- 每项扫描都记耗时和最近成功时间；失败的那一项记错误、时间戳不动。
- 排队数和最久等待从数据库读；空队列为 0。
- 模型调用按提供方分；Run 结束和中断恢复在事务**提交时**才计数，回滚的不算。
- 工具调用按种类分，沙箱获取分冷启动和被拒，工作区提交成功计数。
- controller 动作按动作和结果分；准入拒绝计数；沙箱数量和内存从数据库行读。
- 出站代理按放行和拒绝原因计数，不按主机。
- API 按路由模板计时，不按带 id 的实际路径；登录成功和失败计数。
- 指标端口：不带 token 时谁都能读；带 token 时缺 token 或 token 错都是 401；对外的 8000
  端口 `/metrics` 是 404；端口被占用时进程不退出。
- Compose：没有服务发布 9464；监控栈只在 profile 下启动、只在 127.0.0.1 上发布；五个进程都
  拿到 `METRICS_TOKEN`；Prometheus 抓全部五个服务；**告警规则和仪表盘里引用的每个指标名，
  代码里都真的注册了**。
- trace：建 Run 的请求就是它的 trace；时间片、工具、沙箱获取挂在它下面；恢复在同一条 trace
  里；controller 动作通过控制行加入调用方的 trace；经替身 API 发出的 HTTP 工具请求里没有
  `traceparent` 和 `tracestate`；trace 默认关闭；日志在 span 里时带 `trace_id`。

## 走通了什么

在本机的 Compose 栈上，重建到这个分支（`redeploy.sh` 确认四个后端容器都有新代码），设
`METRICS_TOKEN` 和 `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318`，起 `observability` profile。

1. **Prometheus 的七个目标全是 up**：api、worker、scheduler、controller、egress-proxy，加两个
   exporter。从宿主机访问 `127.0.0.1:8000/metrics` 是 404，`127.0.0.1:9464` 连不上。
2. **沙箱拿不到出站代理的指标。**用真实的沙箱镜像起一个容器，接在 `tiny-hermes_sandbox-egress`
   上（沙箱所在的网络），直连出站代理在这个网络上的地址 `:9464`：不带 token 是 401，token 错
   是 401，两次都没有拿到任何指标；带对的 token 是 200。也就是说，**端口沙箱是连得到的**，挡住
   它的是 token。
3. **压测时 Prometheus 和数据库对得上。**2 个 worker × 4 条通道，模型延迟 3 秒，
   `concurrency_probe --rate 2 --seconds 60 --long-tasks 2 --rounds 3`：

   | | 数据库 | Prometheus |
   |---|---:|---:|
   | Run 数 / 领取次数 | 122 | `claim_wait_seconds_count` 122 |
   | 完成 | 122 | `runs_finished_total{status="completed"}` 122 |
   | 带 trace 上下文的 Run | 122 | — |
   | 工具调用 | 2 个长任务 × 3 轮 = 6 | `tool_call_seconds_count{kind="shell"}` 6 |
   | 模型调用 | — | 完成 122 + 要工具 6 |
   | 沙箱 | 2 个长任务各一个 | 冷启动 2；controller acquire 2、freeze 6、thaw 6、destroy 2 |
   | worker 目标 | `--scale worker=2` | `count(up{service="worker"})` 2，通道总数 8 |
   | 同时执行峰值 | 8（按开始、结束时间算） | 忙碌通道最大 6（15 秒采样一次） |

   worker 在扩容时重建，计数器从这次运行开始，所以读的是计数器的原始值。用 `increase()`
   读同一窗口得到 131、135 这样的数，是 Prometheus 按窗口边缘外推的结果，不是多算。
4. **Jaeger 里一个 Run 是一条完整的链。**按一个长任务 Run 行里存的 trace id 去 Jaeger 取：
   21 个 span、3 个服务。api 的 `POST /api/v1/runs` → worker 的 `run.slice`，下面 4 次
   `model.call`、`sandbox.acquire`、3 次 `tool.shell`、3 次 `workspace.commit` →
   controller 的 `controller.acquire`、3 次 `controller.freeze`、3 次 `controller.thaw`、
   `controller.destroy`。每次模型调用约 3001 ms，正是设定的模型延迟。
5. 每项扫描最近一次成功都在 5 秒以内，扫描错误为 0。

测完之后栈已经恢复：1 个 worker、`WORKER_CONCURRENCY=1`、模型延迟 50 ms、不带 token、不开
trace；监控服务已停（镜像留着）；临时的数据库转发容器已删。库里留下了 `probe-*` 工作空间和
这一次的 122 个 Run。

## 发现

1. **忙碌通道的峰值，Prometheus 看到的比实际低。**数据库算出同时执行最多 8 个，Prometheus
   15 秒采一次，最大只看到 6。gauge 看不到两次采样之间的尖峰。要看峰值，用领取等待的直方图
   和最久等待，它们不会漏。
2. **出站代理的指标端口，沙箱是连得到的。**这是 token 方案的前提，不是缺陷：Compose 网络里
   分不清哪个地址属于哪个网络，所以靠 token 而不是靠监听地址（设计 §4.2、§9）。
3. **archive ban 的正则命中了 OpenTelemetry 的 `propagate` 模块上那个提取函数的属性调用。**
   `tests/unit/test_archive_ban.py` 禁止源码里出现这个拼写，本意是禁止解压归档。没有给 ban 开
   例外，而是把函数按名字导入再调用。
4. **trace 上下文写在 Run 行里，和请求头无关。**API 不读客户端带来的 `traceparent`，每个请求
   都是新的 trace；否则任何调用方都能指定 trace id 和是否采样。

## 这一遍没能证明什么

- **没有用真实模型。**模型是确定性替身，`openai_compatible` 这一路的指标和 span 只在代码里，
  没有在真实提供方下看过；token 计数只在提供方报告了用量时才有。
- **SSE 连接数没有专门的测试**，只在代码里：打开时加一、结束时减一。压测时也没有 SSE 订阅者。
- **流式的 controller 动作（工作区导入、导出）没有 span，`controller_action_seconds` 也不计
  它们。**只有普通的请求-应答动作被计时和追踪。
- **告警规则没有触发过。**只证明了它们引用的指标名都存在、Prometheus 能加载；阈值是初值，
  没有在真实故障下验证会不会报、会不会误报。
- **Grafana 仪表盘只证明了它引用的指标名都存在**，没有截图核对每个面板的显示。
- **B 档（worker 在别的机器上）没有测。**运维手册写了要在企业自己的 Prometheus 里加目标。
- 「OTLP 导出不经过出站代理」是设计决定（设计 §9），没有测试去防止有人把导出目标配成外网
  地址。
- trace 的采样率只在单元测试里核对了配置；没有在 0.1 下实际跑过，看「没被采样的 Run 完全
  没有 span」。controller 和恢复路径在没有上游 trace 时不建 span，这一点有代码，没有专门测试。
- 环境是 macOS 上 8 核的 OrbStack 虚拟机，不是 §24.1 的参考机。每个配置只跑了一次。

## 不声称什么

- 不声称指标和 trace 的开销可以忽略。没有对比开关前后的吞吐。
- 不声称告警阈值合适。它们是初值，要试点之后写进 §24。
- 不声称 Prometheus 的数字精确等于数据库。计数器对得上；gauge 是采样，会漏掉两次采样之间的
  变化。
- 不声称这是生产可用的监控部署。Compose 里的监控栈是给本地和小规模部署的：Grafana 默认密码
  要换，Prometheus 只保留 15 天，没有高可用。

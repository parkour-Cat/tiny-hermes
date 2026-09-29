# 一个进程跑 K 个 Worker（`WORKER_CONCURRENCY`）

分支 `feat/worker-concurrency`，代码截至 `4694c01`。建在 `perf/worker-no-feishu-sdk`
之上。未推送、未合并。

## 为什么有这一遍

[并发探针](2026-09-29-concurrency-probe.md)量出：一个 Worker 进程同一时刻只执行一个 Run，
而它几乎一直在等模型（CPU 峰值 < 8%）。并发只能靠加进程，每个进程是一份固定开销：
去掉飞书 SDK 之后约 96 MiB，之前约 220 MiB。所以并发是拿内存换的。

## 这次改动替产品做的决定

- **默认 `WORKER_CONCURRENCY=1`，范围 1–64。** 调大它，同一台 Docker 主机上同时存在的
  沙箱也会跟着变多（每个上限 1 CPU、1 GiB）。这是容量决定，交给运维，不做成默认值。
- **一个进程里的 K 个 Worker 只共享数据库连接池和对象存储。** 每个 Worker 有自己的 id、
  Redis 订阅、沙箱客户端和模型路由。共享 Redis 订阅是错的：`RedisWakeUpNotifier.wait`
  用 `get_message` 读订阅，读到唤醒的那个 Worker 会把它拿走，其余的只能睡满轮询间隔。
- **连接池随 K 变大。** K=1 时保持 SQLAlchemy 默认的 5 + 10，和以前一样；K>1 时是
  K + K。一个 Worker 同时最多拿两条连接：时间片的事务和租约续期。按默认的 15 条，8 个
  Worker 就会用光。
- **不改设计文档。** 每个 Worker 仍有自己的 id 和租约，§21.3 里 Worker 的语义没有变；
  §5 与 §6 不承诺的是多副本和 K8s，这次两样都不涉及。

## 测试过了

| 套件 | 结果 |
|---|---|
| 后端单元（全部） | 2449 passed |
| 后端集成 `tests/integration/runs` | 523 passed |
| `ruff check` / `pyright`（改动文件） | 通过 / 0 errors |

两个套件是在 rebase 到 `ecf2359` 之前跑的。rebase 只在下面插进了一个文档提交，代码没变。

rebase 之后在本机重跑 `tests/unit/scripts`，有一条红：`test_benchmark_m1.py::test_shape_only_exits_nonzero_when_the_host_is_too_small`，报 `FileNotFoundError: .git/refs/heads/feat/worker-concurrency`。原因是 git 把 ref 打包进了 `packed-refs`，而 `benchmark_m1.git_sha()` 只读散落的 ref 文件。切回 `main` 跑同一条，同样失败，所以跟这次改动无关。这一遍没有修它。

先写测试并看它红（`e884de8`，7 条失败）：

- 设置项的默认值和边界。
- `_worker()` 按配置建出 K 个 runtime：id 各不相同，Redis 订阅各不相同；K 个同时跑起来；
  退出时每个订阅都关掉；连接池按 K 设定。
- 集成测试：3 个真实的 `WorkerRuntime` 共享一个 loop 和一个连接池，3 个 Run 在同一时刻
  执行，全部完成。**这一条在改动之前就是绿的**，文件的 docstring 写明了：它钉住的是
  新接线所依赖的一个性质（Worker 的状态不放在模块级），不是驱动新代码的测试。

## 走通了什么

本机开发栈用 `redeploy.sh` 重建到 `d6179d5`（rebase 之后是 `226c421`，内容相同），四个服务都起来了，scheduler 的飞书长连接
连上了。探针的负载模式和[第 0 步](2026-09-29-concurrency-probe.md)一致：开环到达，
每个 Run 一个 Session，确定性模型，延迟 3 秒。

| 配置 | 负载 | 完成 | 吞吐 | 排队 P95 / 最大 | 执行时间 P95 | 同时执行 峰值 / 平均 | Worker 内存合计 | CPU 峰值 | 数据库连接 |
|---|---|---:|---:|---|---:|---|---:|---:|---:|
| 改动前：8 个 Worker 容器 | 2/s × 60s | 120/120 | 1.89/s | 0.52 / 0.55 s | 3.07 s | 8 / 5.7 | 1773 MiB | 7.2%（每个） | 14 |
| 1 个容器，K=8 | 2/s × 60s | 120/120 | 1.89/s | 0.19 / 0.52 s | 3.07 s | 8 / 5.7 | **87 MiB** | 10.8% | 13 |
| 1 个容器，K=32 | 8/s × 60s | 480/480 | 7.51/s | 0.01 / 0.07 s | 3.05 s | 25 / 22.8 | **102 MiB** | 32.8% | 38 |

读法：

1. 同样 8 个并发，Worker 内存从 1773 MiB 降到 87 MiB（其中一部分来自上一个分支去掉了
   飞书 SDK）。吞吐和排队都没有变差。
2. K=32、四倍负载时，一个约 100 MiB 的进程同时执行了 25 个 Run，只用了一个核的 33%。
   执行时间 P95 没有变长（3.05 s），说明共享的事件循环没有成为瓶颈。
3. 32 个 Worker 在这个负载下用不满：到达率 8/s 乘以 3.03 s 约等于 24 个，和峰值 25
   对得上。

### 故障演练，在 `WORKER_CONCURRENCY=4` 下

- `scripts/restart_drill.py` 四个场景全部通过（124 s）：执行中重启 Worker；Redis 停掉再
  启动；Worker 持有租约时被 kill、由 scheduler 回收；沙箱还活着时 Worker 被 kill，演练
  结束后没有残留容器。
- 另写了一个一次性脚本（没有提交）。一个 Agent 用 `continue_once` 场景，4 个 Run 各在自己的
  Session 里，确认 4 个同时处于 `running` 之后 `docker compose kill worker`，再 `start`。
  结果 4 个全部 `completed`，kill 之后 26.2 s 全部完成，其中大部分时间是在等 20 s 的
  租约过期。

测完已经恢复成 1 个 Worker、`WORKER_CONCURRENCY=1`、50 ms 延迟。

## 推算（不是实测）

在确定性模型下，每个同时执行的 Run 大约占 1.3% 个核：32.8% 除以约 25 个。按这个比例，
一个进程大约在 70 个并发时占满一个核。真实模型要解析流式响应、数 token，每个 Run 的 CPU
会更多，所以真实的上限更低，要重新测。内存方面，从 K=8 到 K=32，合计只多了 15 MiB，
大约每个并发 Run 1 MiB。

## 这一遍没能证明什么

- **没有用真实模型。** CPU 的推算只对替身成立。
- **K 个并发的 Run 都没有用工具。** 所以没有测过「一台 Docker 主机上同时有 K 个沙箱」。
  演练第 4 个场景只有一个沙箱。
- 没有测多个容器、每个容器 K 个的组合，也没有测 K=64。
- **没有测优雅退出。** 进程收到 SIGTERM 时，K 个时间片会一起收尾；
  `worker_shutdown_grace_seconds` 是否够用，没有在 K>1 下验证。
- 没有 SSE 订阅者。
- **没有取得 compose-e2e 结果。** 分支没有推送，CI 没有跑。

## 不声称什么

- 不声称默认部署现在就能支撑高并发。默认值仍然是 1。
- 不声称 K 可以随便设到 64。上限只是设置项允许的范围，实测只到 32，而且没带沙箱。
- 不声称数据库连接数在多容器下还是线性的。

## 探针也跟着改了

`scripts/concurrency_probe.py` 现在会从容器环境读 `WORKER_CONCURRENCY`，输出里同时有
容器数和 Worker 总数（`b49ae9c` 测试，`4694c01` 实现）。改之前，一次 K=32 的运行会被
记成 `workers: 1`。上表三行的 JSON 都是改之前采集的，K 值来自启动命令。

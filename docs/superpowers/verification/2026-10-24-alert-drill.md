# 告警演练，以及 Worker 配置建议的更新

日期 2026-10-24。分支 `ops/worker-sizing-and-alert-drill`。

## 一、Worker 配置建议（运维手册 §8.2）

只改了文档。补上 10 月的两次测量：确定性模型下一个进程 32 条通道就到 90% 到 95% 的 CPU，证实了
K = 16 的建议；真实模型下每条短消息约 0.22 核·秒，和通道数无关。所以真实模型下进程数按 CPU 算
（每个进程每秒约 3 条短消息，留三成余量），通道数按并发算，手册里给了算法和一个例子。

带工具的长任务每轮的 CPU 没有单独测过，手册里写明了。

## 二、告警演练

告警规则从 v2.18 加上以来，**一条都没有真的响过**。

### 规则测试

新增 `deploy/compose/observability/alerts.test.yml`，用 `promtool test rules` 跑：9 条规则（原有 7
条，加上下面新增的 2 条）每条都有「条件满足时会响」的用例，大部分还有「差一点时不响」的用例。CI 的
backend-unit 里加了这一步；另有一个仓库测试要求每条告警在用例文件里至少出现一次。

写用例时自己犯过两次错，都是测试数据的问题：

- 两条 `increase(...[1m])` 规则，测试数据按每分钟一个点给，一分钟的窗口里只有一个点，算不出增量。
  改成和 `prometheus.yml` 一样每 15 秒一个点。
- 「目标消失」用了 `_`（缺失），Prometheus 会把最后一个值沿用 5 分钟。真实情况是目标离开服务发现时
  会写入陈旧标记，改成 `stale`。

### 真的触发

本机 Compose 栈加监控（Prometheus 每 15 秒抓一次），每 30 秒读一次告警状态：

| 怎么触发 | 结果 |
|---|---|
| 每分钟约 46 次错误密码登录（不同账号） | `LoginFailuresSpiking` 1 分钟后 pending，**3 分钟后 firing** |
| 停掉所有 Worker，提交一个任务 | `QueueWaitingTooLong` 1.5 分钟后 pending，**6.5 分钟后 firing** |
| 停掉 scheduler | **5 分钟里没有任何告警** |

**停掉 scheduler 不会触发任何告警**，原因有两层：

1. Prometheus 按服务名（DNS）找目标。容器停了，名字解析不到，30 秒后 scheduler 从目标列表里整个消失，
   不是显示为「down」，所以 `up == 0` 也看不到它。
2. 读 scheduler 指标的告警（扫描停滞）没有数据可算；`QueueWaitingTooLong` 读的排队指标也是 scheduler
   发布的，一起没了。所以负责恢复任务的那个进程死了，反而是最安静的。

**修法**：新增两条告警。

- `ProcessMissing`：api、worker、scheduler、controller、egress-proxy 任何一个一个目标都没有（`absent()`），
  持续 2 分钟。
- `ProcessDown`：目标还在列表里但抓不到数据（`up == 0`），持续 2 分钟。

**修完再演练**：停掉 scheduler，30 秒后 `ProcessDown` pending（目标还在），之后目标消失、换成
`ProcessMissing` pending，**3.5 分钟后 firing**；scheduler 重新启动后告警自动消失。

## 测试过了

- `promtool test rules`：SUCCESS（9 条规则的用例全部通过）。
- 后端单元测试 2755 passed（这个分支没有改后端代码，只改了测试、告警规则、CI 和文档）。

## 这一遍没能证明什么

- 只真的触发过 3 种情况。`WorkerLanesSaturated`、`SchedulerScanStalled`（进程活着但某项扫描一直失败）、
  `SchedulerHousekeepingStalled`、`SandboxAdmissionRefusing`、`EgressDenialsSpiking` 只有规则测试。
- 告警只到 Prometheus 的「firing」，没有接 Alertmanager，没有验证通知能不能送到人。
- `ProcessMissing` 写死了单机 Compose 里的五个服务名。B、C 档下别的机器上的进程不在这个 Prometheus 里，
  要在企业自己的监控里另配。
- 从进程停止到告警 firing 要 3.5 分钟：30 秒左右目标消失，再持续 2 分钟，加上 15 秒的评估间隔。

## 不声称什么

- 不声称告警阈值合适。它们都是初值，要在试点之后定。

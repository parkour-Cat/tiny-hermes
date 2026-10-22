# 一台主机开几个 Worker：写进运维手册，顺带修停止和连接数

日期 2026-10-21。分支 `docs/workers-per-host`。对应[容量方案](../plans/2026-09-29-enterprise-capacity.md)
§5 第 4 项。

| 步骤 | 测试 | 实现 |
|---|---|---|
| 停止时交还 Run、宽限期生效、Compose 等 30 秒 | `480f9a5`、`7c895bc` | `b4e1ec8` |
| `POSTGRES_MAX_CONNECTIONS` | `2939ed3` | `747ba01` |
| 运维手册 §8 | — | 本记录所在提交 |

## 为什么有这一遍

第 4 项原本只是「把建议值写进运维文档」。写之前要回答两件事：开多个 Worker 之后，停一个
Worker 会怎样；连接数够不够。两件都有问题。

## 发现

1. **停止 Worker 时，它手上的 Run 会被中断。**
   - M1 运行设计 §6.5 写的是：SIGTERM 之后跑完当前这一轮，时间片结束，交还租约，所以滚动
     重启让 Run 干净地回到 `queued`；超过宽限期才放弃，交给租约过期。
   - 实现是：`run_once` 不看停止信号，要跑完整个时间片（最长 30 秒）；
     `worker_shutdown_grace_seconds` 从 M1 起就只是一个设置项，没有任何代码读它；
     Compose 默认 10 秒就 SIGKILL。
   - 后果：一次重启，K 个 Run 大多被中断，每个用掉 3 次恢复机会里的一次。K 越大，一次重启
     波及的 Run 越多。这正是第 4 项要建议调大的那个数。
   - 又是一个「写进去了不等于有人够得着」：设计、设置项、配置表都在，只差读它的那行代码。
2. **连接池上限之和超过 `max_connections`。**按单机建议值：2 个 Worker 容器 × 32 + API 120 +
   Scheduler 15 + controller 15 = 214，Postgres 允许 200。单机档验收最多只用到 70 条，所以
   没人看见；数据库一慢，池涨到上限时才会出事。

## 测试过了

- 后端单元测试全部通过；ruff、pyright 0 errors。
- 新测试：
  - 集成测试：Worker 在第二轮期间收到停止信号，第二轮跑完后不再开新一轮，Run 回到 `queued`，
    沙箱冻结保留，没有 `run_interrupted`。时间片设成 300 秒，所以只有停止信号能结束它。
  - 单元测试：宽限期内跑完的 Worker 会被等到；超过宽限期还在跑的被取消，返回不超过 2 秒；
    Worker 进程用的是设置里的宽限期（20）。
  - Compose：`stop_grace_period` 大于 `WORKER_SHUTDOWN_GRACE_SECONDS` 的默认值；
    `max_connections` 由 `POSTGRES_MAX_CONNECTIONS` 设定。
- `docker compose config` 确认：Worker 主机的 compose 文件经 `extends` 继承了
  `stop_grace_period: 30s` 和宽限期；`POSTGRES_MAX_CONNECTIONS=300` 时 Postgres 的参数是 300。

## 走通了什么

单机栈，确定性模型，延迟 1 秒，`WORKER_CONCURRENCY=4`。镜像是在已有镜像上覆盖
`packages/backend/src` 得到的（完整重建时 `uv sync` 下载依赖超时，见 CLAUDE.md 的代理问题；
依赖没有变化）。

**4 个长任务都在跑时 `docker compose restart worker`：**

| | 重启用时 | `run_interrupted` | 用掉的恢复次数 | 最后 |
|---|---:|---:|---:|---|
| 改之前 | 10.6 s | 3 | 1、0、1、1 | 4 个都完成 |
| 改之后 | 2.4 s | 0 | 0、0、0、0 | 4 个都完成 |

改之前的 10.6 秒就是 Compose 等满 10 秒再杀。

**一轮比宽限期长：**工具里执行 `sleep 60`，进入命令 5 秒后重启。重启用了 20.7 秒，Worker
日志有 `workers still busy after the shutdown grace period, leaving their leases to lapse
grace_seconds=20`，然后 `worker stopped`。那个 Run 的事件：`run_interrupted`、
`run_recovery_approved`、新沙箱里重新执行，最后 `run_completed`。和设计说的一样：超过宽限期
就交给租约过期。

测完已恢复成 `WORKER_CONCURRENCY=1`、延迟 50 ms、调用上限 20。

## 这一遍没能证明什么

- **建议值本身没有重新测。**K = 16、N = 核数 ÷ 4 来自单机档验收，那次用的是确定性模型。真实
  模型每条通道要多少 CPU，还是不知道。
- 停止只测了 K = 4，没有测 K = 16、32 时 16 个时间片同时收尾要多久。每个时间片收尾要冻结
  沙箱、写一次库，K 很大时可能超过 20 秒。
- `POSTGRES_MAX_CONNECTIONS` 调大之后，没有在连接真的打满的情况下测过，也没有测
  Postgres 在几百条连接下的内存。
- 没有测 B 档下滚动重启 Worker 主机。
- 宽限期到了以后取消的是整个进程里还在跑的所有 Worker；一个取消掉的时间片会不会留下写了
  一半的状态，靠的是「每次写都在事务里、取消时事务回滚」，这一遍没有专门构造这种情况。

## 不声称什么

- 不声称按手册的建议值就能撑住真实负载。手册写明了换上真实模型后要重测。
- 不声称停止 Worker 不会再中断 Run。一轮比宽限期长的时候，照样会中断，只是现在是 Worker
  自己按设计放弃，而不是被 Compose 杀掉。

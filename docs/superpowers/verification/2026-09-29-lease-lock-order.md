# 租约续期与时间片记录的死锁

分支 `fix/lease-lock-order`，建在 `feat/fair-claim` 之上，但只改了
`runs/infrastructure/sql_store.py` 的 `renew_lease` 并加了一个新测试。这两个提交
cherry-pick 到 `main` 上没有冲突（已在临时 worktree 里试过），但没有在 `main` 上单独
跑过测试。未推送、未合并。

## 现象

2026-09-29，32 路并发长任务，64 个里有 1 个在 `workspace_checkpoint_failed` 之后被
中断，恢复后失败（[公平领取与长任务](2026-09-29-fair-claim-and-long-tasks.md)第 1 条）。
同一时刻 Postgres 报 `deadlock detected`，日志里的两条语句是：

- `renew_lease` 的 `UPDATE runs SET last_heartbeat_at …`：它已经持有租约行；
- `record_slice` 的 `SELECT … FROM worker_leases … FOR UPDATE`：它已经持有 Run 行。

## 复现

`tests/integration/runs/test_lease_lock_order.py` 强制出现生产上碰巧撞上的那种交错：

1. 一个闸门事务锁住租约行；
2. 续租在租约行上排队；
3. 记录时间片拿到 Run 行，然后在租约行上排队；
4. 闸门提交，租约先给了续租，续租接着要 Run 行，而 Run 行在记录时间片手里，于是死锁。

每一步都用 `pg_stat_activity` 确认对方已经在等锁，才走下一步。用的都是真实的
`SqlRunStore.renew_lease` 和 `record_slice`。

修复前连跑 3 次，3 次都是 `DeadlockDetectedError`，失败的一方都是记录时间片，也就是
生产上输掉的那一方。

## 原因

对四个假设逐一检验，结论是加锁顺序反了：

- 同时动这两行的路径有四条：`record_slice`、`reserve_tool_call`、`reclaim_expired_lease`、
  `renew_lease`。前三条都是先 Run 后租约，只有 `renew_lease` 是先租约后 Run。
- Worker 的 `_lease_lock` 把「直接记录时间片」和「续租」互斥开了，但工作区提交
  （`SessionWorkspaceService.checkpoint` → `SqlWorkspaceLedger.commit` →
  `record_slice`）是在这把锁之外调用的。
- `renew_lease` 的 docstring 写着「条件本身就是并发控制，不需要行锁」。对租约行来说成立，
  但同一个事务接着会更新 `runs`，那一步会隐式加行锁。

「只要让工作区提交也拿 `_lease_lock`」这个假设被排除了：复现测试里根本没有 Worker 进程，
照样死锁。进程内的锁挡不住数据库层面的顺序反转。

## 修复

`renew_lease` 先 `_lock_run`，再更新租约和心跳。如果 Run 不属于这个工作空间，就返回
`None`。docstring 改成写明加锁顺序和原因。

## 测试过了

| 套件 | 结果 |
|---|---|
| `test_lease_lock_order.py`（修复后连跑 5 次） | 5/5 passed，每次 < 1 s |
| 后端单元（全部） | 2464 passed |
| 后端集成 `runs` + `session_workspace` | 568 passed |
| `ruff` / `pyright`（改动文件） | 通过 / 0 errors |

修复后，复现测试里两个事务仍然都进入了锁等待，只是按同一顺序排队。所以测试真实地
制造了争用，不是没碰到锁就通过了。

## 走通了什么

本机栈重建到 `cb6a651`，并在容器里核对过修复代码已经在里面。然后用
`WORKER_CONCURRENCY=32`、每轮模型延迟 1 秒，跑 3 轮 32 个长任务（每个 10 轮）：

| 项 | 结果 |
|---|---|
| Run | 99/99 completed（96 个长任务，3 条短消息） |
| Postgres 日志里的 `deadlock detected` | 0 |
| `run_interrupted`、`workspace_checkpoint_failed`、`run_failed`、`run_recovery_approved` 事件 | 0 |

测完已恢复成 1 个 Worker、50 ms 延迟。

## 这一遍没能证明什么

- **现场运行不是这个修复的证据。** 原来的出错率大约是 64 个里 1 个，96 个没出错在统计上
  很弱。证据是那条强制交错的测试；现场运行只说明修复没有带来新问题，比如续租先等 Run 锁
  之后租约过期。
- 没有检查 sessions 锁的顺序。工作区提交是 sessions → runs，直接记录时间片是
  runs → … → sessions。这两条对同一个 Run 不会并发发生（同一时间片内是先后执行的），
  但这一点是读代码得出的，没有测试钉住。
- **没有取得 compose-e2e 结果。**

## 不声称什么

- 不声称这个库里已经没有别的死锁。这一遍只检查了同时动 `runs` 和 `worker_leases` 的四条
  路径。

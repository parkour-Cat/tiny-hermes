# 一次 compose-e2e 偶发失败,记下来而不是调大超时 — 2026-08-24

> 现场:PR #50,commit `18dbf8c3`。同一个 commit 跑了两次 CI。

## 事实

| 运行 | 触发 | compose-e2e |
|---|---|---|
| 32652990292 | push | **failure** |
| 32652998806 | pull_request | pass |

失败的那次,**Playwright 是 18 条全过**。挂的是它后面的 `Workspace drill` 这一步:

```
== a committed write survives a worker crash ==
  $ docker compose kill worker
  $ docker compose start worker
worker did not become healthy within 90s
```

`scripts/restart_drill.py:84` 的 `await_healthy(timeout=90.0)`;worker 的健康检查是
`process-alive`(`grep -q tiny-hermes /proc/1/cmdline`,interval 5s)。

## 判断

**和 PR #50 无关。** 那条 PR 只动 `apps/web`(一个搜索框和一个对话框),没有碰
worker、compose 文件或任何演练脚本。最近 25 次 CI 运行里,compose-e2e 只失败过
这一次。

## 为什么没有把 90 秒调大

调大超时能让这个红变绿,而且**看不出**是掩盖了什么。如果 worker 在共享 runner 上
偶尔真的要一分半才起来,那是一件值得知道的事;把预算改成 180 秒之后,下一次它花了
两分钟,同样不会有人知道。

所以这一遍只记录,不改。**它再出现一次,就值得真正去查**:先拿到失败那次
`docker compose logs worker` 的输出,确认是进程起得慢,还是健康检查在容器还没
`start` 完成时就开始计时。

## 这一遍没能证明什么

- **没有拿到那次失败时 worker 容器自己的日志**。GitHub 的 job 日志里只有演练脚本
  的输出,而演练在超时后直接 `SystemExit`,没有把容器日志打出来。
  这本身是演练的一个缺口——**一次说不清原因的失败,和一次没发生的失败,对读日志
  的人来说差别不大**。
- 因此「进程起得慢」和「健康检查计时起点不对」这两种解释,**这一遍都没有排除**。

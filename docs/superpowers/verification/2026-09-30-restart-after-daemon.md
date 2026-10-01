# `dockerd` 重启之后，服务自己回来

日期 2026-09-30。分支 `feat/capacity-acceptance`，未推送。测试 `da2f0b7`，修复 `8e2c0ef`。

## 为什么有这一遍

[OOM 记录](2026-09-30-sandbox-oom.md)：OOM killer 杀掉 `dockerd` 之后，所有容器都停了。
因为全是 `restart=no`，连 Postgres 在内没有一个自己回来。

## 改了什么

- compose 里 10 个长期运行的服务都加了 `restart: unless-stopped`：postgres、redis、seaweedfs、
  api、controller、worker、scheduler、egress-proxy、web、chat-web。
- 用 `unless-stopped` 而不用 `always`，是为了让运维人员主动停掉的服务保持停止。
- `migrate` 不加。它是一次性任务，正常退出码是 0；给它加重启策略会让迁移循环执行。
- 测试：`test_compose_restart.py` 逐个服务检查策略，另外检查 `migrate` 没有策略。修复前 10 条
  红、1 条绿，修复后 11 条全绿。

## 走通了什么

**1. `docker kill` 不会触发重启。** CI 的重启演练用 `docker compose kill worker` 模拟 Worker
挂掉，再检查租约过期后 Run 回到队列。如果 `docker kill` 触发了自动重启，演练的前提就变了。
- 用一个 `--restart unless-stopped` 的小容器试：`docker kill` 之后，它停在 `exited`，重启次数 0。
- 对照：从 VM 这一层 `kill -9` 它的进程（这才是崩溃），3 秒内重新运行，重启次数 1。

**2. 像 OOM 那次一样杀掉 `dockerd`。** 用一个特权容器从 VM 这一层 `kill -9` 了 `dockerd`：

| 距离杀掉 | 10 个服务的状态 |
|---|---|
| +2 秒 | Docker 已经应答，10 个都是 `starting` |
| +7 秒 | 10 个都是 `healthy` |
| 之后 90 秒 | 一直都是 10 个 `healthy` |

- 所有服务的启动时间都是 12:31:43，也就是杀掉之后 1 秒。
- 对照：同一台守护进程上的测试库 `th-test-pg` 没有重启策略，停在 `Exited (255)`，和 OOM 那次
  一样。
- api 和 Worker 的重启次数都是 0，没有因为「Postgres 还没就绪」而退出过。
- 接着跑了一次小探针：12 个 Run 全部完成，其中 2 个是要建沙箱的长任务。

**3. CI 的两个演练在本机照样通过。** 按 compose-e2e 的步骤，在带重启策略的栈上跑：
- 重启演练：4 个场景全部通过，125 秒；
- 工作区演练：通过；
- 没有残留的沙箱容器。

## 这一遍没能证明什么

- 只在 OrbStack 上验证过。`dockerd` 是谁重新拉起来的（OrbStack 还是它的 init），没有查；在
  Linux 主机上要由 systemd 负责，这一遍没有测。
- 只杀过一次 `dockerd`。
- 这只解决「停了能自己回来」，不解决「内存被用满」。内存用满时，服务会反复被杀、反复重启，
  这种情况没有测。
- 没有测 Docker 的 `live-restore`。

## 不声称什么

- 不声称服务在任何启动顺序下都能正常起来。这一次它们是同时启动的，而且都没出问题；
  但这只是一次观察。

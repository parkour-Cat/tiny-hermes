# 沙箱把主机内存用完时，整个栈会停掉：OOM 杀掉了 dockerd

日期 2026-09-30。分支 `feat/capacity-acceptance`，未推送。栈的代码是 `f323fe7`。这是一次会把
本机的栈打挂的测试，事先征得了同意。

## 为什么有这一遍

[沙箱内存](2026-09-30-sandbox-memory.md) 那份记录得出：所有沙箱内存的总和没有任何上限。
但超过物理内存之后会发生什么，那一遍没有测。

## 环境

- OrbStack VM：`MemTotal` 8005 MiB。swap 有两块：
  - zram 8 GiB（lz4 压缩）；
  - 磁盘 swap 1 GiB。
- 所有服务的重启策略都是 `restart=no`，`oom_score_adj` 都是 0。compose 里没有写 `restart:`。
- 负载和沙箱内存那份记录相同：2 × 16 条通道，24 个长任务（每个 60 轮），再加每秒 3 条短消息，
  持续 150 秒。每个沙箱用 `cache=M` 在 tmpfs 里常驻 M MiB 随机数据。
- 测试前：栈空闲时 `MemAvailable` 约 5.9–6.1 GiB。

## 第一步：每个沙箱 250 MiB（合计约 6 GiB）

合计刚好超过空闲时的可用内存。

| 时刻 | 沙箱 | MemAvailable | swap 已用 | Shmem |
|---|---:|---:|---:|---:|
| 开始 | 0 | 5943 MiB | 74 MiB | 125 MiB |
| +约 9 秒 | 24 | 1459 MiB | 1563 MiB | 3610 MiB |
| +约 18 秒 | 24 | 447 MiB | 5900 MiB | 1217 MiB |
| 之后一直 | 24 | 最低 186 MiB | 最多 7623 MiB | 约 200 MiB |

- **没有 OOM。** 474 个 Run 全部完成，没有失败。
- 但明显变慢：
  - 短消息排队 P95 8.3 秒、最多 10.1 秒；端到端 P95 10.7 秒（同一负载不放数据时是 2.1 秒）；
  - 长任务端到端 P95 231 秒（原来 175 秒）。
- tmpfs 里的页被换出到了 swap，Shmem 于是降下来了。
- 事后看 zram 的统计：`mem_used_max` 是 6.78 GB。**随机内容在 zram 里几乎压缩不了，换进
  zram 并不腾出内存。**真正多出来的，只有 1 GiB 磁盘 swap，加上各服务里能压缩的那部分
  （事后沙箱都已删掉，zram 里剩下的应是服务的页，压缩比约 3.4 : 1）。

## 第二步：每个沙箱 400 MiB（合计约 9.6 GiB）

| 时刻（UTC） | 沙箱 | MemAvailable | swap 已用 |
|---|---:|---:|---:|
| 12:04:00 | 0 | 6091 MiB | 1175 MiB |
| 12:04:04 | 18 | 4819 MiB | 1116 MiB |
| 12:04:13 | 24 | 160 MiB | 8370 MiB |
| 12:05:48 起 | —— | 什么都读不到 | —— |

内核日志（缓冲区已经滚动，最早的几次被杀的记录丢了）里，能看到至少 4 次 `invoked oom-killer`，
都是 `global_oom`。被杀的进程依次是：

1. 34 个 `nginx`：web 和 chat-web 的工作进程；
2. 沙箱里正在写数据的 `head` 和 `bash`，各 2 个；
3. OrbStack 自己的组件：`serialclient`、`hidserver`、2 个 `udevd`；
4. 沙箱的 `sleep` 18 个、`docker-init` 15 个；
5. OrbStack 的 `diskclient`；
6. **`dockerd`**（`oom_score_adj` 是 −500，本来是受保护的）。

第一次能看到的 OOM 时，所有进程的匿名内存加起来只有约 50 MB（`active_anon` 6 MB 加
`inactive_anon` 44 MB），可用 swap 还剩 453 MiB。
**内存不在任何一个进程里**：它在 zram 里，是被换出去的 tmpfs 页。OOM killer 杀进程释放不了
它们，于是把能杀的都杀了，最后杀到了 `dockerd`。

`dockerd` 死掉后被重新拉起，但所有容器都以退出码 255 停了下来：Postgres、api、Worker、
controller、scheduler、seaweedfs、redis、web、chat-web、egress-proxy 全部停止，连同一台守护
进程上的测试库 `th-test-pg` 也停了。它们的 `State.OOMKilled` 都是 false：这些容器不是各自
被 OOM 杀掉的，是因为 `dockerd` 死了才停。因为是 `restart=no`，**没有一个自己回来**。

对用户的影响：从内存耗尽（开始后约 13 秒）起，探针提交的 450 条短消息**一条都没有建成**。

## 恢复

用默认配置 `docker compose up -d`：

- Postgres 报 `database system was not properly shut down`，崩溃恢复用了 0.49 秒，之后正常
  接受连接。
- 崩溃时正在执行的 24 个长任务：
  - 每个都有一条 `run_interrupted` 和一条 `run_recovery_approved`，先后拿了 2 次租约，最后
    全部 `completed`；
  - 工具调用一共 1440 次，正好是 24 × 60；模型轮次一共 1464 次，正好是 24 × 61。**没有
    任何一轮被重复记录。**
  - 每次拿到租约都有一条 `sandbox_cache_reset`（一共 48 条）：换了沙箱，cache 从空开始，
    和设计一致。
- 残留的 24 个已退出的沙箱容器，被 controller 清理掉了。

## 读法

1. **超过可用内存之后，先是变慢，再往上就是整体宕机，中间没有缓冲。**在 zram 上，随机内容的
   swap 腾不出内存。第一步慢了 5 倍但还活着；第二步只多了 3.6 GiB，13 秒内就耗尽了。
2. **OOM killer 帮不上忙。**占内存的是 tmpfs 页，不属于任何进程，杀进程释放不了。沙箱的
   `sleep` 和 `docker-init` 都被杀了，但要释放它们的 tmpfs，还得先把容器拆掉；而这时
   `dockerd` 自己也快撑不住了。
3. **`dockerd` 是单点。**它一死，所有容器都停，包括 Postgres；再加上 `restart=no`，整个栈就
   一直停着。
4. **平台自己的恢复是好的**：只要把服务重新拉起来，Run 一个不丢，也没有重复执行的轮次，
   残留的沙箱也会被清理。问题出在「不让它发生」和「自己重新拉起来」这两层。

## 这说明要改什么（提议，都还没做、没测）

- **不让它发生**：controller 按「已建沙箱的内存上限之和」做准入，超过主机的预算就让新沙箱
  排队。这是几条里唯一能防止这种情况的一条。同时把 `memswap_limit` 设成等于内存上限，
  让单个沙箱的压力留在它自己的 cgroup 里。
- **缩小爆炸半径**：给沙箱设 `oom_score_adj=1000`，给 Postgres 设负值。这只能决定先杀谁；
  这一遍里沙箱的进程已经被杀了，还是没能保住 `dockerd`。所以单靠它不够。
- **能自己回来**：生产用的 compose 给服务加 `restart: unless-stopped`。在 Linux 主机上可以再开
  Docker 的 `live-restore`，让 `dockerd` 重启时容器不跟着停。这在 OrbStack 上没有验证过。

## 这一遍没能证明什么

- 内核日志缓冲区滚动了，**最早被杀的是谁不知道**。
- 只在 OrbStack 的 VM 上测过，它的 swap 是 zram。换成用磁盘 swap 或者没有 swap 的 Linux
  主机，表现会不同：没有 swap 时，第一步可能就已经 OOM 了。
- 占内存的是 tmpfs 里的文件，不是进程内存。如果是某个沙箱里的进程自己占了大量内存，
  OOM killer 会先挑它（它的 RSS 最大），结果可能只死那一个沙箱。所以这一遍是 tmpfs 这条路的
  情况；Agent 往 `/tmp` 或 cache 里写大文件是真实会发生的事。
- 上面的改法一个都没有测。
- 每一步只跑了一次。
- `dockerd` 是谁重新拉起来的（OrbStack 还是它的 init），没有查。

## 不声称什么

- 不声称在 Linux 生产主机上 `dockerd` 也会被杀。那取决于 `dockerd`、`containerd` 和各服务的
  `oom_score_adj`，以及主机的 swap。
- 不声称上面任何一条改法有效。

测完之后，栈已经恢复成 1 个 Worker、`WORKER_CONCURRENCY=1`、模型延迟 50 ms、调用上限 20；
`th-test-pg` 也已经重新启动。没有遗留的沙箱。

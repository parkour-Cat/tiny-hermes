# C 档验收：多台沙箱主机

日期 2026-10-24。[C 档设计](../specs/2026-10-22-tier-c-sandbox-host-pool-design.md)，产品设计 v2.23
（§1.23）。四个 PR：

| 步骤 | 内容 | 验收记录 |
|---|---|---|
| 第 2 步 | 准入、驱逐、调用都按主机分开 | [第 2 步](2026-10-23-tier-c-step2-host-admission.md) |
| 第 3 步 | 主机登记、心跳、地址池不重叠 | [第 3 步](2026-10-23-tier-c-step3-host-registry.md) |
| 第 4 步 | `SANDBOX_CONTROLLER_URL=pool`：放置和路由 | 本文 |
| 第 5 步 | `scripts/sandbox_hosts.py`：list、drain、activate、retire；运维手册 §16 | 本文 |

## 第 4、5 步的测试

- 放置（单元测试，假的主机目录和假的主机）：Run 已有沙箱时只去原主机，原主机满了也不换；新沙箱按
  放置顺序；「内存不够」和「没连上」换下一台；**请求可能已经发出去的不换**；别的拒绝直接返回；全满
  是「内存不够」，全连不上是「没连上」，没有可用主机也按「内存不够」排队；没有地址的主机算没连上；
  其他调用按沙箱所在主机转发；沙箱所在主机只查一次，刚申请的沙箱不用查。
- 放置的数据库查询（真 Postgres）：剩余内存多的在前（预算减去已预约）；跳过 `draining`、`retired`、
  心跳超时、没有地址的主机；按 Run 和按沙箱查到所在主机。
- Worker：收到 `reservation_elsewhere` 时 Run 回到队列，不失败。
- 下线（真 Postgres）：retire 只释放这台的沙箱、实例标为已销毁、状态变 `retired`；再 retire 一次
  释放 0 个；名字写错报错，不会悄悄什么也不做；drain 返回剩余沙箱数；activate 放回来；list 显示状态和
  已预约内存。
- **端到端**（真 Postgres、真 Scheduler）：一个 Run 的沙箱在一台连不上的主机上，Scheduler 清理失败，
  它保持 interrupted、沙箱 isolated；retire 那台之后，Scheduler 的下一轮把它放回队列。
- 脚本：retire 不带 `--confirm-host-is-gone` 时拒绝执行，并说明原因。
- 设置：`pool` 必须配 TLS 证书；心跳超时默认 30 秒。
- 已有的一条测试按新参数改了：worker 进程的 CLI 测试里替换 `_controller` 的桩函数多了一个参数。
- 先写测试、看它失败、再实现。例外：心跳循环的测试是写完代码后补的。第 5 步的测试第一次运行时
  失败的原因有两处是测试自己写错了（没有登记主机、一个辅助函数缺类型），改了测试，实现没改。

## 现场验收

**环境**：8 核 / 7.8 GiB 的 OrbStack VM，确定性替身模型（第 2 个场景起每轮固定延迟 3 秒）。

**怎么模拟两台主机**：

- **主机 A**：主栈，`compose.yaml + compose.sandbox-host.yaml + compose.sandbox-pool.yaml`，名字 `sbx-a`，
  地址池 `10.240.0.0/16`。
- **主机 B**：另一个 Compose 项目，只跑 controller 和 egress-proxy，名字 `sbx-b`，地址池 `10.241.0.0/16`。
- 两台都是每个沙箱 256 MB、预算 2048 MB，每台最多同时 8 个。
- 两个 Worker 进程，每个 16 条通道，和 Scheduler 一起用 `SANDBOX_CONTROLLER_URL=pool`，经双向 TLS 调用
  两台 controller。
- **和真实部署不同的地方**：两台共用一个 Docker 和一个网络；Worker 按容器名连各台 controller（同一个
  网络上 `controller` 这个服务名会解析到两台）。

### 1. 两台都登记上了

`sandbox_hosts` 里 `sbx-a`、`sbx-b` 都是 `active`，地址、预算、地址池和启动参数一致，心跳都在 5 秒内。
旧的 `default`（这台机器以前的名字，没有地址）被放置跳过；用脚本 retire 它：不带确认参数时脚本拒绝
执行，带上之后显示 `0 sandboxes released`。

### 2. 放置和准入：40 个带工具的长任务

同时开 40 个长任务（每个 6 轮 `shell.exec`）：

| | sbx-a | sbx-b |
|---|---:|---:|
| 落在这台的 Run | 24 | 16 |
| 同一时刻最多的活沙箱 | **8**（2048 MB） | **8**（2048 MB） |

- 40 个全部完成，0 失败。
- 两台都在各自预算的上限上，**从来没有超过**；两台都满时，Worker 记了 6 次 `memory_budget_exhausted`，
  等待后重试。
- **每个 Run 只有一个沙箱实例**。其中 24 个 Run 跑了 2 到 3 个时间片：之后的时间片解冻的都是原主机上
  原来那个沙箱，没有一个换了主机或新建沙箱。

### 3. 一台主机挂掉

16 个长任务（每个 12 轮，每轮 3 秒），第 15 秒时 A 上有 8 个活沙箱、B 上 8 个，这时停掉 A 的 controller：

- **60 秒后，这 8 个 Run 没有一个在 B 上建了沙箱**：状态都是 `interrupted`，沙箱记录都是 `isolated`。
  这正是设计要的：A 只是连不上，平台分不清它是死了还是网络断了，宁可等。
- 执行 `retire sbx-a --confirm-host-is-gone`：`8 sandboxes released`。
- 之后 **7 个在 B 上冷恢复并完成**（它们在 B 上各有一个新实例）。
- **1 个失败了**，原因是替身模型的剧本：A 停掉的那一刻，这个 Run 第 4 轮的命令已经在 A 上跑完，但随后
  提交工作区失败，平台按设计回滚了这一轮，告诉模型 `rolled back: workspace_checkpoint_failed`（退出码
  126）。替身模型的剧本遇到任何失败的工具结果都会直接判失败（`deterministic_long_task_command_failed`）。
  真实模型看到回滚后可以重做这一轮，但**这一遍没有用真实模型验证**。

### 4. 下线的主机连回来

重新启动 A 的 controller：它重新登记，但状态**仍是 `retired`**，心跳照常；它自己的清扫删掉了 1 个残留
容器和 1 个卷。其余残留已经被 B 的定期清扫删掉了：两台共用一个 Docker，已销毁的行对谁来说都是「没有
记录」。真实部署里各台只扫自己的 Docker，只会是 A 自己删。

### 5. 排空

`activate sbx-a`、`drain sbx-b`（输出 `4 live sandboxes left`，是之前任务留下的保温沙箱），再开 10 个
长任务：**10 个全部落在 A 上**，全部完成。B 上保温的沙箱按 `SANDBOX_IDLE_TTL_SECONDS` 到期后被回收：
最后一个在 03:27:52 到期，03:27:56 时 `sbx-b` 的活沙箱数降到 0，可以停机维护。

## 这一遍没能证明什么

- **没有多台真实的机器。**两台「主机」共用一个 Docker 和一个网络，测的是放置、路由、按主机准入和下线
  流程，不是网络隔离、带宽，也不是冷恢复跨机器下载工作区的成本。
- **真实模型遇到「这一轮被回滚」会不会重做**，没有验证（场景 3 里失败的那 1 个）。
- 只停了 controller，沙箱容器还在同一个 Docker 上；没有模拟整台机器断电。
- 没有测 Scheduler 在一台主机连不上时，另一台的回收是否照常。单元层面每个 reservation 的清理是各自捕获
  异常的，但没有现场走过。
- 单机部署（`compose-e2e`）由 CI 验证，见 PR。

## 不声称什么

- 不声称能撑多少人：主机池让带工具的并发随主机数增长，但 Postgres 仍然只有一个，这份验收没有测它。

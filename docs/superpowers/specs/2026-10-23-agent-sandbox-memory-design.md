# 沙箱内存按 Agent 配置

日期 2026-10-23。产品设计 v2.22（§21.3）。起因是[真实模型长任务压测](../verification/2026-10-23-real-task-probe.md)。

## 1. 要解决什么

每个沙箱的内存上限是实例级的一个数（`SANDBOX_MEMORY_MB`，默认 1024），准入按这个数预约。
压测里 20 个写 bash 脚本的任务，每个沙箱实际只用几 MiB，却各预约 1 GiB，主机预算 4 GiB 时
同时只能开 3 个，其余排队：总耗时 85 秒。把实例默认值改成 256 MB 后是 25 秒。

但实例默认值是给所有 Agent 的：调低了，装依赖、编译、跑数据处理的 Agent 会被内存上限杀掉；
不调，轻量 Agent 白白占着并发。**轻重由 Agent 决定，所以这个数应该写在 Agent 上。**

## 2. 设计

### 2.1 Agent 上的字段

AgentSpec 新增可选的顶层文档：

```json
"sandbox": { "memory_mb": 256 }
```

- 不写 `sandbox`（或写了但没写 `memory_mb`）：用实例默认值 `SANDBOX_MEMORY_MB`，和现在一样。
- `memory_mb` 下限 256（和 `SANDBOX_MEMORY_MB` 的下限相同），模型上**没有上限字面量**。理由和
  `AgentLimits.max_model_calls` 一样：已发布版本的文档每次 Run 开始都要重新解析，管理员调低上限
  不能让已经发布的版本解析失败。上限在**写入时**检查。
- 没写时规范化后不带这个键，已发布版本的内容哈希不变。

为什么不放进 `limits`：`limits` 的每个字段都有默认值，规范化后总会出现在文档里。加一个字段就会
改变所有已发布版本的哈希。

### 2.2 上限

- 新实例配置 `SANDBOX_CEILING_MEMORY_MB`，默认 4096：Agent 可以要的最大值。
- 启动时检查：上限不小于 `SANDBOX_MEMORY_MB`（默认值不能超过上限）。
- 上限**可以**大于沙箱内存预算。预算没配置时是主机内存的一半，只有 controller 知道，各台主机还
  可能不一样；要求上限不大于预算，会让默认的 4096 在一台 8 GiB 的主机上启动不了。代价由
  controller 在申请时承担：一个沙箱要的比整个预算还多，就永远放不进来。这时不按
  `memory_budget_exhausted` 让 Run 排队（它会一直排下去），而是拒绝，原因 `memory_out_of_range`。
- 保存草稿和发布时，`memory_mb` 超过上限返回 422 `sandbox_memory_ceiling_exceeded`，和其他
  上限一样。
- controller 自己再校验一次：worker 送来的值不在 [256, 上限] 内时拒绝，原因
  `memory_out_of_range`，Run 失败（`sandbox_memory_out_of_range`）。正常路径走不到这里。这一条
  兜的是改上限之前就已经发布了大值的版本，以及上限只在 controller 上调低的情况。

### 2.3 传递

- worker 申请沙箱时，在 `acquire` 里带上 `memory_mb`（Agent 没写就不带）。
- controller 用默认档位，把内存换成请求的值。准入预约、容器的内存上限、`sandbox_instances.memory_mb`
  都用这个值。
- 解冻同一个 Run 自己的保温实例时不重新申请，沿用实例创建时的值。Run 执行中途 Agent 发布新版本
  不影响已经在跑的 Run：Run 用的是开始时的那个版本。
- 新 worker 对旧 controller：旧 controller 不读 `memory_mb`，退回实例默认值。不会出错，只是不生效。

### 2.4 控制台

Agent 详情页的「限制」里加一项「沙箱内存（MB）」，可以留空，留空表示用实例默认值。

## 3. 换不来什么

- **不会自动选大小。**写多少由 Agent 作者决定；写小了，沙箱里的进程会因为内存不足被杀掉，
  命令失败，输出里有提示。按实际用量自动调整或超卖，要先有真实工作负载的内存分布，这一次不做。
- tmpfs（`/tmp` 256 MB、`/workspace/cache` 512 MB）的大小不跟着变。它们写进的数据算在容器的内存
  上限里，所以 256 MB 的沙箱实际上写不满 768 MB 的 tmpfs，写到内存上限时进程会被杀掉。这和现在
  一样：1024 MB 的沙箱也只有 1024 MB 给进程和 tmpfs 共用。
- 只按 Agent 配，不按工作空间配。

## 4. 验收

- 单元测试：规范化不带空的 `sandbox`；哈希不变；写入时超过上限被拒；controller 按请求的值预约
  并设置容器内存上限；超出范围被拒；比整个预算还大的请求被拒，而不是排队。
- 集成测试：真 Docker 上，Agent 要 256 MB 的沙箱，容器的内存上限确实是 256 MB，准入按 256 计。
- 用 `real_task_probe` 再跑一次：实例默认值保持 1024，Agent 写 256，20 个任务的耗时应接近 25 秒。
  如果没有接近，说明这个值在某一段没有传到。

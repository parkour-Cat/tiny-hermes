# 沙箱按需惰性分配实施计划（Lazy Sandbox Allocation）

> 日期：2026-10-12  
> 状态：已实施（PR #113，[验收记录](../verification/2026-10-21-lazy-sandbox-allocation.md)）  
> 目标分支：`feat/lazy-sandbox-allocation`  
> 关联文件：`packages/backend/src/tiny_hermes/runs/application/worker.py`、`docs/superpowers/plans/2026-09-29-enterprise-capacity.md`

## 1. 为什么有这份计划

在当前实现中，`worker.py:466` 存在**过度悲观预分配（Eager Allocation）**问题：
* 在进入 Agent 循环调用模型前，只要 Agent 的静态配置里绑定了任何非平台工具（`any(tool not in PLATFORM_TOOLS for tool in first.tools)`），Worker 就会立即调用 `_open_sandbox` 申请/占用一个沙箱容器。
* **业务矛盾**：一个全功能的编程助理或企业运维 Agent，必然绑定了 `shell.exec`、`file.read` 等工具。但员工找它日常沟通、咨询方案、澄清需求或打招呼（“你好”）时，大模型根本不需要调用工具，本可以直接返回纯文本。
* **灾难后果**：因为预分配机制，哪怕发一句“你好”，也会死死占用宿主机 1 GiB 的沙箱内存配额。在单机档（总沙箱预算约 3~4 个）或高并发场景下，海量原本无须沙箱的简单对话把沙箱名额全部占满，导致需要跑代码的长任务与普通短消息互相阻塞，单机容量被人为大幅压低。

## 2. 这份计划替产品做的决定

1. **调用模型前不预建沙箱**：
   * 移除 `worker.py:466` 在时间片开始时的强制 `_open_sandbox` 调用。
   * 首轮模型调用以无容器状态（`box = None`）轻量发起。
2. **惰性按需激活（Lazy Allocation）**：
   * 在模型返回后，检查 `response.stop_reason` 与 `response.tool_calls`。
   * 若模型仅返回纯文本（无工具调用），**全流程不启动、不解冻沙箱**，直接完成本轮，沙箱开销为 0。
   * 当且仅当模型输出了非平台工具（如 `shell.exec`、`file.*`）时，才触发 `_ensure_sandbox` 动态向 Controller 申请新建或解冻沙箱。
3. **环境重置提示（Hint）的移位**：
   * 原有 `sandbox_cache_reset` 的 hint 是在时间片开始前注入。改为：若沙箱是本轮首次惰性激活且发生过重置，在工具调用结果或下一轮的系统补充信息中提示模型，保证 Prompt 语义一致。
4. **沙箱准入受限时的失败/退避语义**：
   * 若模型要求执行工具，但此时沙箱内存耗尽（`memory_budget_exhausted`）：
     * 本轮模型调用用量如实记账（避免累计费用未知）；
     * 本轮生成的工具调用不予执行，任务安全结束时间片退回 `queued` 并进入 10 秒冷静退避期；
     * 绝不静默失败，也不破坏会话历史。

## 3. 实施步骤（遵循 TDD 规范）

- [x] **Step 1：测试先行（红）**
  * 新增测试用例：为一个绑定了 `shell.exec` 的 Agent 发送纯打招呼消息，断言在整个 Run 完结过程中，`ControllerClient.acquire` 调用的次数为 0，沙箱实例数为 0。
  * 在当前代码上运行，该测试应当为 FAIL（因为当前代码在调用模型前就调用了 acquire）。
- [x] **Step 2：解耦时间片入口逻辑**
  * 改造 `worker.py` 的 `_execute_slice`：初始化时将 `box = None`，移除顶层的静态 `_open_sandbox`。
- [x] **Step 3：实现惰性激活辅助函数 `_ensure_sandbox`**
  * 在 `_answer_tools` 真正处理非平台工具调用之前，检查 `box` 是否已就绪；若为空，按需执行 `acquire`。
- [x] **Step 4：处理准入异常与退避回滚**
  * 完善 `SandboxRefusal.MEMORY_BUDGET_EXHAUSTED` 在工具分发阶段的处理，确保模型回复不进历史，任务优雅退回队列。
- [x] **Step 5：全量回归测试**
  * 跑通后端单元测试、沙箱集成测试（`pytest packages/backend/tests/integration/runs`）；
  * 确认现有长任务（多轮工具连续执行）、冻结与解冻（Thaw）、工作区快照提交（SessionWorkspace）测试不受破坏。
- [x] **Step 6：并发压测验收**
  * 使用 `scripts/concurrency_probe.py` 模拟混合负载：在 3 个长任务占满沙箱时，并发注入 10 条纯聊天消息，断言短消息排队 P95 降至 0.0x 秒，证明纯消息不再被沙箱配额阻塞。
  * 实际做法见验收记录：探针的「排队时间」只算到第一次被领取，被拒后重新排队的时间看不到，所以改看端到端时间（6 个长任务 + 120 条短消息，P95 140 s → 2.1 s）。

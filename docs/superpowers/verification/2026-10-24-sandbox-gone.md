# 沙箱容器没了时，Run 立刻被打断并恢复

日期 2026-10-24。分支 `fix/sandbox-gone-reason`。

## 问题

工具调用时如果沙箱容器已经不在（被外部删掉，或者已经停止）：

- Docker 返回 404 或「容器没有运行」。controller 把它当成 `DockerUnavailable`，按类名回给 worker。
- 适配层不认识这个名字，原样抛出 `ControllerClient.Refused`。这一片按「意外异常」失败，日志写
  `worker slice failed`。
- Run 一直占着租约，要等租约过期，scheduler 才发现它并打断。压测里，从失败到打断用了一分多钟。

当初考虑过把它映射成已有的拒绝原因，作为工具调用被拒告诉模型。但沙箱已经没了，这一片里后面的调用会接连
失败，所以没有这样做。

## 改了什么

- **引擎：** `exec_create` 失败之后才去查一次容器状态。
  - 不存在：报 `ContainerGone`，这是新增的；
  - 已停止：报 `ContainerNotRunning`，原来只有冻结时会报。

  调用成功的正常路径不多一次查询。
- **适配层：** 这两个名字转成新的 `SandboxGone`。`_call` 和 `_refusal` 原来各有一份映射逻辑，现在统一走
  `_refusal`。
- **Worker：** 回答工具调用时收到 `SandboxGone`，就写一条警告，然后立刻把这一片记为打断。
  - 被打断这一轮的模型调用照常计费；
  - 它的工具调用不保留，免得下一片看到一个没有结果的调用。

  这和 acquire 时 controller 连不上那条路的做法一样。之后由 scheduler 的下一轮扫描恢复：隔离、回收，然后
  在新沙箱里继续。

## 测试过了

- **真 Docker：**
  - 在已杀掉的容器上执行，报 `ContainerNotRunning`；
  - 在已删掉的容器上执行，报 `ContainerGone`。

  改之前两条都失败。runc 和 runsc 下各跑一遍。
- **单元测试：** `ContainerGone`、`ContainerNotRunning` 经过适配层变成 `SandboxGone`。
- **集成测试：** 假沙箱在执行时报 `SandboxGone`，Run 变成 `interrupted`，没有 `run_failed`；模型调用计 1 次，
  对话里只剩用户消息。改之前测试失败，异常一路抛出。
- **全量：**
  - 单元测试 2767 passed；
  - 非沙箱集成测试 1135 passed；
  - 沙箱集成测试 303 passed、1 failed。失败的那条是 runsc 下模拟手动删除冻结沙箱的测试，它的修正在 #154，
    这个分支还没有。
- **本机栈：** worker 的模型延迟设成 3 秒，跑 8 轮长任务，在第 2 次工具调用之后删掉沙箱容器。

  | 时间 | 事件 |
  |---|---|
  | 04:54:13 | 删掉容器 |
  | 04:54:15.5 | 下一轮工具调用时 Run 被打断（中间是 3 秒的模型延迟） |
  | 04:54:16.2 | 恢复被批准 |
  | 04:54:19.4 | 新沙箱建好 |
  | 04:54:35 | Run 完成 |

  worker 日志是 `sandbox gone mid-slice, interrupting ... reason=ContainerGone`，没有 `worker slice failed`。
  工具调用记了 7 次：long_task 按轮次计数，被打断那一轮算了一轮，但它的工具调用没有保留。

## 这一遍没能证明什么

- 只覆盖了非流式和流式执行里 `exec_create` 这一步。命令已经开始运行后容器才消失时，命令会被杀掉，返回 137，
  这一点没有改。
- 恢复次数是有上限的。反复被删的沙箱会让 Run 用光恢复次数，这个上限的行为没有在这里测。
- 本机测的是 runc；runsc 下这条路径只在集成测试里走过。

## 不声称什么

- 不声称所有「Docker 出错」都能这样恢复：只处理了容器不存在、容器已停止这两种。Docker 守护进程本身连不上时，
  仍然是原来的失败路径。

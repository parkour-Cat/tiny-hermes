# 流式执行中途失败时，客户端收到的是真实原因

日期 2026-10-24。分支 `fix/stream-failure-mid-send`。由[两小时压测](2026-10-24-long-task-soak.md)发现。

## 问题

压测里沙箱容器被删掉时，worker 报的是 `stream_frame_too_large`。原因：

- controller 的流式动作（如 `execute_stream`）在 `start_send` 之后才去 `docker exec`。容器正好在这两步之间
  消失时，异常发生在已经开始发帧之后。
- 服务端把这个失败照常写成一行 JSON 回答（`{"error": ...}`），而客户端还在按帧读。它把 `{"er` 这 4 个字节
  当成帧长度（约 20 亿），报了 `stream_frame_too_large`。
- 真实原因丢了，适配层没法把它映射成 `SandboxRefused`。

协议里本来就有 `ERROR` 帧类型，接收方也认得它，但服务端从来没发过。

## 改了什么

- 服务端：发送进行中（`START` 之后、`END` 之前）出错时，先发一个 `ERROR` 帧，再照常写回答行。发送结束后，
  保活的 `PROGRESS` 帧不再发。
- 客户端：收到 `ERROR` 帧后停止读帧，去读回答行，从中拿到真实原因。`CANCEL` 帧仍然报 `stream_cancelled`。

## 测试过了

- 真 Unix socket，真服务端和真客户端：服务端发了一部分数据后抛出具名拒绝（`sandbox_unavailable`）或者一般异常
  （`RuntimeError`），客户端分别收到这两个原因（**改之前两条都失败，收到的是 `stream_frame_too_large`，和压测
  里一样**）。
- 沙箱集成测试 215 passed、1 skipped；后端单元测试 2763 passed。
- 本机栈（controller 和 worker 换成这个分支的镜像）：一次正常的流式执行完成，退出码 0；执行中途删掉容器，表现和
  改之前一样（命令被杀，退出码 137；下一次调用报 `ContainerNotRunning`，Run 被打断后继续）。

## 这一遍没能证明什么

- **没有在真实栈上复现压测里那个失败。** 它要求容器正好在 `start_send` 和 `docker exec` 之间的几毫秒里消失。
  试过在执行中途删除容器、在容器出现时立刻暂停容器，都落不进这个窗口。所以这条修复在真实栈上的证据只有
  「没有改坏正常路径」，复现靠的是上面的 socket 测试。
- 拿到真实原因之后，worker 的处理不一定不同：`DockerUnavailable`、`ContainerNotRunning` 这些不是
  `RefusalReason`，适配层仍然原样抛出，这一个切片仍会失败，Run 走打断再恢复的路径。改的是报错说的是真话。

## 不声称什么

- 不声称流式协议的其他失败情况都已处理：这次只处理了服务端在发送途中出错这一种。

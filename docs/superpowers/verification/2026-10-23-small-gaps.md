# 两个小缺口：controller 的失败原因、成员接口的行级安全

日期 2026-10-23。分支 `fix/small-gaps`。

| 改了什么 | 测试 | 实现 |
|---|---|---|
| controller 遇到不是「拒绝」的失败时，把失败的原文记进自己的日志 | `e76d030` | `23bac71` |
| 成员接口受行级安全约束（证明已经如此，并更正记录） | `20840d9` | 无需改代码 |

## controller 的失败原因

**为什么有这一遍。**PR #126 的第一次 CI，工作区配额演练失败：scheduler 清理沙箱时，controller
每次都回 `DockerUnavailable`。controller 只把异常的类名传回给调用方，两边都没有记 Docker 的
原话，所以查不出 Docker 为什么拒绝。

**怎么改的。**controller 回给调用方的内容不变，仍然只有拒绝原因或类名：Docker 的原话里可能
有主机、路径、容器 id，不该传给调用方。新增的是 controller 自己的日志：只要失败的异常不带
`reason`（不是平台自己做出的拒绝），就记一条 WARNING，带上动作名、类名、原文和堆栈。
平台自己做出的拒绝（比如 `lease_invalid`）照旧不记，因为那是正常的决定，不是故障。

**测试过了。**新测试让替身 controller 抛出一个带原文的异常：

- 调用方收到的仍然是 `RuntimeError` 这个类名；
- 日志里有一条 WARNING，内容里有原文，`action` 是 `cleanup`。

Unix socket 和 TLS 两种传输都跑了。修改前，两种传输下都是红的。

## 成员接口的行级安全

PR #127 的验收记录和运维手册 §14 都写着：「用路径参数指定工作空间的成员管理接口不受限」。
**这句话是错的。**成员接口用 `_require_path_matches_header` 核对路径和请求头，而它调用的正是
`require_workspace_id`，也就是请求进入工作空间的那一处。写那句话时，只按「路径参数」这个形式
猜了一下，没有读代码。

新测试：管理员属于两个工作空间（先用超级用户核对，数据库里一共 2 条成员记录）。把成员列表
查询的工作空间条件去掉，再请求其中一个工作空间的成员列表，接口只返回 1 行。这条测试一写好
就是绿的：它证明的是已有的行为，不是新改的。两份文档已经更正。

## 测试过了（全量）

- 后端单元测试 2682 passed；pyright 0 errors；`ruff check` 通过。
- 集成测试（含 sandbox 目录）1256 passed、1 skipped、1 failed：
  `sandbox/test_controller.py::test_a_command_that_outruns_its_timeout_is_stopped_with_what_it_started`。
  - 这条测试直接调用 `SandboxController`，用真实 Docker 跑一个超时命令，再固定等 1 秒，检查
    命令有没有被停掉。它不经过这一遍改动的传输层服务端。
  - 单独重跑 3 次都通过。判断是负载下的时序问题，没有修。
- compose-e2e 以 PR 的 CI 为准。

## 这一遍没能证明什么

- 没有在真实的 Docker 故障下看过这条日志。测试里的失败是替身抛的 `RuntimeError`，不是真实的
  `DockerException`。真实情况下，`DockerUnavailable` 的原文就是 Docker SDK 异常的文本。
- 流式动作（工作区导入导出）的失败也走同一个 `_refusal`，但没有单独为它写测试。

## 不声称什么

- 不声称找到了 #126 那次 CI 失败的原因。这一遍只保证下次再发生时，日志里有原文。

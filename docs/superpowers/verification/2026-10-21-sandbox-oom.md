# 沙箱内存超限之后，它的 Run 会怎样

日期 2026-10-21。容量路线图 P1 第 9 项。测量为主，只修了发现 3。

## 为什么有这一遍

§21.3 禁止沙箱用 swap 以后，沙箱的内存超过上限时，由它自己 cgroup 里的 OOM killer 处理。
9-30 的[准入记录](2026-09-30-sandbox-admission.md)里，写满 tmpfs 让整个容器被杀了。
但 Run 那边会看到什么，一直没测过。

## 怎么测的

本机 Compose 栈：1 个 Worker，K=1，确定性模型。场景用 `shell_from_input`：Run 的输入就是一条
命令，命令失败（退出码非 0）就判 Run 失败。用一个临时的驱动脚本提交命令，再从 `run_events`
和 `session_messages` 读出工具结果，同时记录 Docker 的容器事件。

沙箱用默认配置：内存 1 GiB 且禁止 swap，`/tmp` 是 256 MiB 的 tmpfs，`/workspace/cache` 是
512 MiB 的 tmpfs，容器由 `docker-init` 托管，主进程是 `sleep infinity`。

## 结果

### 1. 命令里有进程超过内存上限（最常见的情况）

命令是 `head -c 2000M /dev/zero | tail -n 1 > /dev/null; echo exit=$?`。`tail` 会把 2000 MB
全部留在内存里。

- **9 次里有 8 次：只有 `tail` 被杀，容器照常运行。**
  - shell 继续往下执行，打印 `exit=137`；
  - bash 自己报告 `Killed`；
  - 工具结果的 exit_code 为 0（因为最后一条命令是 `echo`），Run 正常完成；
  - cgroup 的 `memory.events` 记下 `oom_kill 1`；`docker inspect` 显示 `OOMKilled=true`，
    但容器仍在运行。
  - 用同样的镜像和限制直接 `docker run` 复现，结果一样。
- **9 次里有 1 次：命令整个返回 137，输出为空。**`echo` 没有执行，说明那一次被杀的是 shell，
  或者整个 exec。原因没查清，Worker 和 Controller 的日志里都没有记录。

### 2. 容器在命令执行中途整个死掉

在当前默认配置下，用真实的 OOM 做不出这种情况，原因见下面的「发现」第 1 条。所以改用
`docker kill`（SIGKILL）在命令执行到第 3 秒时杀掉沙箱容器来模拟。命令是
`echo start; sleep 10; echo finished`。

1. 工具结果：exit_code 137，输出只有 `start\n`。
2. 时间片结束时冻结沙箱失败。Worker 日志里是 `freeze failed`，Controller 给出的原因是
   `DockerUnavailable`。
3. 按 M1 沙箱设计 §11「冻结失败 → `interrupted`，绝不悄悄重新排队」，Run 进入 interrupted。
   接着 Scheduler 批准恢复（`replay_safe: true`），Run 被重新领取。
4. 新时间片里，模型读到 exit 137 和 `start`。确定性场景据此判失败；换成真实模型，它会
   自己决定下一步。
5. 被杀 1 秒后，这条 reservation 变成 `released`，实例变成 `destroyed`。没有遗留的容器和
   volume。
6. 同一会话的下一个 Run 正常完成。

## 发现

1. **在默认配置下，内存超限不会让整个沙箱死掉。**两个 tmpfs 加起来是 768 MiB，比 1 GiB 的
   上限小，所以总有一个可以杀的进程，OOM killer 杀的是它，容器本身留下来。9-30 那次整个容器被杀，
   要求 tmpfs 能装下比上限更多的数据。
   - 配置允许这种组合：`SANDBOX_CACHE_MB` 最大 4096，`SANDBOX_MEMORY_MB` 最小 256。
   - 配置和沙箱规格的校验里，都没有检查「tmpfs 合计小于内存上限」：`container_policy.py` 只把每个 tmpfs 各自和上限比较。
2. **模型看不出命令是因为内存不够被杀的。**工具结果里只有 bash 的一行 `Killed` 和退出码 137，
   平台没有补充说明。`memory.events` 的 `oom_kill` 计数是现成的，可以在命令前后各读一次来判断。
   这次没有做。
3. **冻结失败的原因名称有误导性。**已修**（测试 `dea2949`，实现 `aaf5428`）：
   引擎把 Docker 的每一种错误都叫 `DockerUnavailable`，包括「容器已经停了」的 409。现在
   `pause` 失败时会再查一次容器状态，容器还在但已停止时报 `ContainerNotRunning`。它是
   `DockerUnavailable` 的子类，所以处理方式不变（冻结失败 → interrupted），变的只是名字。
   - 集成测试：对真实 Docker 杀掉容器再冻结，报 `ContainerNotRunning`。沙箱集成测试 109 条通过。
   - 现场重做「执行中杀容器」：Worker 日志变成 `Refused: ContainerNotRunning`，Run 照旧
     中断再恢复。传给 Worker 的只有类名，异常里带的退出码和是否 OOM 没有传过去，也没有记进日志。
4. **事件顺序：`run_interrupted` 排在 `tool_called` 前面。**不只是这条路径：状态变化和这一轮的
   事件在同一个事务里写入时，`_decide_and_write` 总是先写状态事件，再写这一轮的事件，所以
   每个 Run 的最后一轮都是这样（例如 `run_completed` 排在那一轮的 `model_round` 前面）。
   - 不会丢事件：事件流要等 Run 结束**并且**所有事件都送完才关闭。
   - 没有改：改了会影响所有 Run 的时间线，值得单独做。

第 1、2 条没有修，需要产品层面决定：第 1 条是否要在准入时加检查，第 2 条是否要给模型加提示。

## 这一遍没能证明什么

- **「整个容器死掉」是用 `docker kill` 模拟的，不是真实的 OOM。**真实 OOM 杀掉整个容器时，
  Docker 的事件、退出状态、冻结报的错可能都不一样。
- 那 1 次「整个命令返回 137、输出为空」，原因没有找到，也没能复现。
- 每个 Run 都会新建沙箱：确定性场景每轮只有一条命令，Run 结束沙箱就销毁。所以「同一个沙箱
  OOM 之后，下一条命令还能不能用」没有测到。
- 只在 OrbStack（cgroup v2）上测过。

## 不声称什么

- 不声称 OOM 之后 Run 的结局总是正确的：结局取决于模型怎么理解退出码 137，而这一遍用的是
  确定性模型。
- 不声称调整内存或 tmpfs 配置之后，结果仍然一样（见第 1 条发现）。
